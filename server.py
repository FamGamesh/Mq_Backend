from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import os
from dotenv import load_dotenv
import requests
import asyncio
import json
from playwright.async_api import async_playwright, Browser, BrowserContext
import subprocess
import sys
import time
import threading
import signal
import atexit

from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.lib.colors import HexColor, black, darkblue, darkgreen, darkred, white, lightgrey
from reportlab.graphics.shapes import Drawing, Rect, Line
from reportlab.platypus.flowables import Flowable
from typing import List, Dict, Optional
import uuid
from datetime import datetime, timedelta
import re
from pathlib import Path
import logging
import pickle
import hashlib

def get_pdf_directory() -> Path:
    """
    Environment-aware PDF directory configuration.
    
    Returns appropriate PDF storage directory based on environment:
    - Production/Cloud environments: /tmp/pdfs (ephemeral but writable)
    - Development environments: /app/pdfs (persistent in development)
    
    Environment detection checks:
    1. Common cloud platform environment variables
    2. Directory writability tests
    3. Filesystem characteristics
    """
    # Check for common cloud environment indicators
    cloud_env_indicators = [
        'DYNO',              # Heroku
        'RENDER',            # Render
        'VERCEL',            # Vercel
        'RAILWAY_ENVIRONMENT', # Railway
        'GOOGLE_CLOUD_PROJECT', # Google Cloud
        'AWS_LAMBDA_FUNCTION_NAME', # AWS Lambda
        'AZURE_FUNCTIONS_ENVIRONMENT', # Azure Functions
    ]
    
    is_cloud_environment = any(os.getenv(indicator) for indicator in cloud_env_indicators)
    
    # Check if we're in a containerized environment
    is_container = os.path.exists('/.dockerenv') or os.path.exists('/proc/1/cgroup')
    
    # Test if /app directory is writable (development environment characteristic)
    app_dir_writable = False
    try:
        test_file = Path("/app/.write_test")
        test_file.touch()
        test_file.unlink()
        app_dir_writable = True
    except (PermissionError, OSError):
        app_dir_writable = False
    
    # Determine appropriate directory
    if is_cloud_environment or is_container or not app_dir_writable:
        # Use /tmp for cloud/production environments
        pdf_dir = Path("/tmp/pdfs")
        print(f"🌤️  Using cloud-compatible PDF directory: {pdf_dir}")
    else:
        # Use /app/pdfs for local development
        pdf_dir = Path("/app/pdfs")
        print(f"🏠 Using development PDF directory: {pdf_dir}")
    
    # Ensure directory exists
    try:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        print(f"✅ PDF directory ready: {pdf_dir}")
    except Exception as e:
        print(f"❌ Error creating PDF directory {pdf_dir}: {e}")
        # Fallback to /tmp if /app fails
        if pdf_dir != Path("/tmp/pdfs"):
            print("🔄 Falling back to /tmp/pdfs...")
            pdf_dir = Path("/tmp/pdfs")
            pdf_dir.mkdir(parents=True, exist_ok=True)
            print(f"✅ Fallback PDF directory ready: {pdf_dir}")
        else:
            raise
    
    return pdf_dir

# Load environment variables
load_dotenv()

# Set Playwright browsers path
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Browser installation state
browser_installation_state = {
    "is_installed": False,
    "installation_attempted": False,
    "installation_error": None,
    "installation_in_progress": False
}

# PERSISTENT JOB STORAGE - Survives server restarts
class PersistentJobStorage:
    """Persistent storage for job progress that survives server restarts"""
    
    def __init__(self):
        self.storage_file = "/tmp/job_progress.pkl"
        self.jobs = {}
        self.load_jobs()
    
    def load_jobs(self):
        """Load jobs from persistent storage"""
        try:
            if os.path.exists(self.storage_file):
                with open(self.storage_file, 'rb') as f:
                    self.jobs = pickle.load(f)
                print(f"🔒 Loaded {len(self.jobs)} jobs from persistent storage")
            else:
                self.jobs = {}
                print("🔒 No persistent storage found, starting fresh")
        except Exception as e:
            print(f"⚠️ Error loading jobs from storage: {e}")
            self.jobs = {}
    
    def save_jobs(self):
        """Save jobs to persistent storage"""
        try:
            with open(self.storage_file, 'wb') as f:
                pickle.dump(self.jobs, f)
        except Exception as e:
            print(f"⚠️ Error saving jobs to storage: {e}")
    
    def update_job(self, job_id: str, status: str, progress: str, **kwargs):
        """Update job progress with automatic persistence"""
        try:
            if job_id not in self.jobs:
                self.jobs[job_id] = {
                    "job_id": job_id,
                    "status": status,
                    "progress": progress,
                    "total_links": 0,
                    "processed_links": 0,
                    "mcqs_found": 0,
                    "pdf_url": None,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat()
                }
            
            self.jobs[job_id].update({
                "status": status,
                "progress": progress,
                "updated_at": datetime.now().isoformat(),
                **kwargs
            })
            
            # Auto-save after each update
            self.save_jobs()
            
            print(f"📊 Job {job_id}: {status} - {progress}")
            
        except Exception as e:
            print(f"⚠️ Error updating job progress: {e}")
    
    def get_job(self, job_id: str) -> Optional[dict]:
        """Get job status"""
        return self.jobs.get(job_id)
    
    def cleanup_old_jobs(self, hours: int = 24):
        """Clean up jobs older than specified hours"""
        try:
            cutoff_time = datetime.now() - timedelta(hours=hours)
            jobs_to_remove = []
            
            for job_id, job_data in self.jobs.items():
                try:
                    updated_at = datetime.fromisoformat(job_data.get('updated_at', ''))
                    if updated_at < cutoff_time:
                        jobs_to_remove.append(job_id)
                except:
                    jobs_to_remove.append(job_id)  # Remove malformed jobs
            
            for job_id in jobs_to_remove:
                del self.jobs[job_id]
            
            if jobs_to_remove:
                self.save_jobs()
                print(f"🧹 Cleaned up {len(jobs_to_remove)} old jobs")
                
        except Exception as e:
            print(f"⚠️ Error cleaning up old jobs: {e}")

# Global persistent job storage
persistent_storage = PersistentJobStorage()

# ULTRA-ROBUST Browser Pool Manager with Memory Management
class UltraRobustBrowserPoolManager:
    """
    Ultra-robust browser manager that handles server restarts and memory constraints
    """
    
    def __init__(self):
        self.browser: Optional[Browser] = None
        self.playwright_instance = None
        self.is_initialized = False
        self.lock = asyncio.Lock()
        self.retry_count = 0
        self.max_retries = 5
        self.last_error = None
        self.restart_count = 0
        self.max_restarts = 10
        
    async def initialize(self):
        """Initialize browser with enhanced error handling"""
        async with self.lock:
            if self.is_initialized and self.browser:
                try:
                    # Test if browser is still alive
                    contexts = self.browser.contexts
                    if self.browser.is_connected():
                        return
                except:
                    pass
                
                print("⚠️ Browser connection lost, reinitializing...")
                await self._cleanup()
                self.is_initialized = False
            
            if self.is_initialized:
                return
            
            print("🚀 Initializing Ultra-Robust Browser Pool Manager...")
            
            # Check browser installation
            if not browser_installation_state["is_installed"]:
                print("⚠️ Browsers not installed, attempting installation...")
                await self._install_browsers()
            
            max_init_attempts = 3
            for attempt in range(max_init_attempts):
                try:
                    self.playwright_instance = await async_playwright().start()
                    
                    # Ultra-conservative browser launch args for maximum stability
                    browser_args = [
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-accelerated-2d-canvas',
                        '--disable-gpu',
                        '--disable-gpu-sandbox',
                        '--disable-software-rasterizer',
                        '--no-first-run',
                        '--no-zygote',
                        '--single-process',
                        '--disable-background-timer-throttling',
                        '--disable-backgrounding-occluded-windows',
                        '--disable-renderer-backgrounding',
                        '--disable-web-security',
                        '--disable-features=VizDisplayCompositor',
                        '--disable-extensions',
                        '--disable-plugins',
                        '--disable-images',
                        '--disable-javascript',
                        '--disable-default-apps',
                        '--disable-background-networking',
                        '--disable-sync',
                        '--no-default-browser-check',
                        '--memory-pressure-off',
                        '--max_old_space_size=256',  # Reduced memory limit
                        '--aggressive-cache-discard',
                        '--disable-hang-monitor',
                        '--disable-prompt-on-repost',
                        '--disable-client-side-phishing-detection',
                        '--disable-component-extensions-with-background-pages',
                        '--disable-component-update',
                        '--disable-breakpad',
                        '--disable-back-forward-cache',
                        '--disable-field-trial-config',
                        '--disable-ipc-flooding-protection',
                        '--disable-popup-blocking',
                        '--force-color-profile=srgb',
                        '--metrics-recording-only',
                        '--password-store=basic',
                        '--use-mock-keychain',
                        '--no-service-autorun',
                        '--export-tagged-pdf',
                        '--disable-search-engine-choice-screen',
                        '--unsafely-disable-devtools-self-xss-warnings',
                        '--enable-automation',
                        '--headless',
                        '--hide-scrollbars',
                        '--mute-audio',
                        '--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4'
                    ]
                    
                    self.browser = await self.playwright_instance.chromium.launch(
                        headless=True,
                        args=browser_args,
                        timeout=30000  # 30 second timeout
                    )
                    
                    # Test browser with a simple operation
                    test_context = await self.browser.new_context()
                    await test_context.close()
                    
                    self.is_initialized = True
                    self.retry_count = 0
                    self.restart_count += 1
                    
                    print(f"✅ Ultra-Robust Browser Pool Manager initialized successfully! (Restart #{self.restart_count})")
                    return
                    
                except Exception as e:
                    print(f"❌ Browser initialization attempt {attempt + 1} failed: {e}")
                    self.last_error = str(e)
                    await self._cleanup()
                    
                    if attempt < max_init_attempts - 1:
                        wait_time = (2 ** attempt) * 2
                        print(f"⏳ Waiting {wait_time}s before retry...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise Exception(f"Failed to initialize browser after {max_init_attempts} attempts")
    
    async def _install_browsers(self):
        """Install browsers with enhanced error handling"""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                browser_installation_state["is_installed"] = True
                print("✅ Browser installation successful")
            else:
                raise Exception(f"Browser installation failed: {result.stderr}")
                
        except Exception as e:
            print(f"❌ Browser installation error: {e}")
            raise
    
    async def get_context(self) -> BrowserContext:
        """Get browser context with optimized error handling and faster initialization"""
        max_attempts = 3
        
        for attempt in range(max_attempts):
            try:
                # Ensure browser is ready
                await self.initialize()
                
                # Create context with optimized settings for speed
                context = await asyncio.wait_for(
                    self.browser.new_context(
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                        viewport={'width': 1280, 'height': 720},  # Optimized viewport for screenshots
                        ignore_https_errors=True,
                        java_script_enabled=False,
                        extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
                        bypass_csp=True
                    ),
                    timeout=15.0  # Reduced from 20.0
                )
                
                print(f"✅ Browser context created successfully (attempt {attempt + 1})")
                return context
                
            except asyncio.TimeoutError:
                print(f"⏱️ Context creation timeout (attempt {attempt + 1})")
                await self._handle_browser_failure()
                
            except Exception as e:
                print(f"⚠️ Error creating context (attempt {attempt + 1}): {e}")
                await self._handle_browser_failure()
                
                if attempt == max_attempts - 1:
                    # Last attempt - try emergency recovery
                    print("🚨 Maximum attempts reached, trying emergency recovery...")
                    await self._emergency_recovery()
                    return await self.get_context()  # One final attempt
            
            # Progressive backoff with faster recovery
            wait_time = min(2 + attempt, 6)  # Faster backoff: 2, 3, 4 seconds max
            print(f"⏳ Waiting {wait_time}s before retry...")
            await asyncio.sleep(wait_time)
        
        raise Exception("Failed to create browser context after all attempts")
    
    async def _handle_browser_failure(self):
        """Handle browser failures with memory cleanup"""
        print("🔧 Handling browser failure with memory cleanup...")
        await self._cleanup()
        self.retry_count += 1
        
        # Force garbage collection
        import gc
        gc.collect()
        
        # If too many failures, wait longer
        if self.retry_count > 3:
            print(f"⚠️ Multiple browser failures ({self.retry_count}), waiting extra time...")
            await asyncio.sleep(10)
    
    async def _emergency_recovery(self):
        """Emergency recovery procedure"""
        print("🚨 Emergency recovery procedure initiated...")
        
        # Force cleanup everything
        await self._cleanup()
        
        # Kill any remaining browser processes
        try:
            subprocess.run(["pkill", "-f", "chromium"], capture_output=True)
            subprocess.run(["pkill", "-f", "chrome"], capture_output=True)
            print("🔫 Killed remaining browser processes")
        except:
            pass
        
        # Clear temporary files
        try:
            subprocess.run(["rm", "-rf", "/tmp/playwright_*"], shell=True, capture_output=True)
            print("🧹 Cleared temporary files")
        except:
            pass
        
        # Force garbage collection
        import gc
        gc.collect()
        
        # Wait before recovery
        await asyncio.sleep(5)
        
        # Reinitialize
        await self.initialize()
    
    async def _cleanup(self):
        """Enhanced cleanup with timeout handling"""
        cleanup_tasks = []
        
        try:
            if self.browser:
                cleanup_tasks.append(self._safe_close_browser())
            
            if self.playwright_instance:
                cleanup_tasks.append(self._safe_stop_playwright())
            
            # Execute cleanup tasks with timeout
            if cleanup_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*cleanup_tasks, return_exceptions=True),
                    timeout=15.0
                )
                
        except asyncio.TimeoutError:
            print("⏱️ Cleanup timeout, forcing termination")
        except Exception as e:
            print(f"⚠️ Error during cleanup: {e}")
        finally:
            self.browser = None
            self.playwright_instance = None
            self.is_initialized = False
    
    async def _safe_close_browser(self):
        """Safely close browser with timeout"""
        try:
            if self.browser:
                await asyncio.wait_for(self.browser.close(), timeout=10.0)
        except:
            pass
    
    async def _safe_stop_playwright(self):
        """Safely stop playwright with timeout"""
        try:
            if self.playwright_instance:
                await asyncio.wait_for(self.playwright_instance.stop(), timeout=10.0)
        except:
            pass
    
    async def close(self):
        """Close browser pool with enhanced cleanup"""
        print("🔫 Closing Ultra-Robust Browser Pool Manager...")
        await self._cleanup()
        print("✅ Ultra-Robust Browser Pool Manager closed")

# Global ultra-robust browser pool
browser_pool = UltraRobustBrowserPoolManager()

def force_install_browsers():
    """Force install browsers with cloud deployment friendly approach"""
    print("🔫 Starting cloud-compatible browser installation...")
    
    try:
        # Ensure directory exists
        os.makedirs("/tmp/pw-browsers", exist_ok=True)
        
        # Set environment variables for installation
        env = os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
        env['PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'] = '0'
        
        # Install system dependencies first
        print("📦 Installing system dependencies...")
        system_deps = [
            "apt-get update -y",
            "apt-get install -y curl wget gnupg lsb-release",
            "apt-get install -y libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgtk-3-0 libgbm1 libasound2",
            "apt-get install -y libxss1 libgconf-2-4 libxtst6 libxrandr2 libasound2 libpangocairo-1.0-0 libatk1.0-0 libcairo-gobject2 libgtk-3-0 libgdk-pixbuf2.0-0",
            "apt-get install -y fonts-liberation libappindicator3-1 libasound2 libatk-bridge2.0-0 libatspi2.0-0 libdrm2 libgtk-3-0 libnspr4 libnss3 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libxss1 libgconf-2-4"
        ]
        
        for dep_cmd in system_deps:
            try:
                subprocess.run(dep_cmd, shell=True, capture_output=True, text=True, timeout=120, env=env)
                print(f"   ✅ {dep_cmd.split()[2] if len(dep_cmd.split()) > 2 else dep_cmd}")
            except:
                print(f"   ⚠️ Failed: {dep_cmd}")
        
        # Simplified installation approaches
        install_commands = [
            f"{sys.executable} -m playwright install chromium --with-deps",
            f"{sys.executable} -m playwright install chromium",
            "python -m playwright install chromium --with-deps",
            "python -m playwright install chromium",
            "playwright install chromium --with-deps",
            "playwright install chromium"
        ]
        
        for cmd in install_commands:
            try:
                print(f"🔫 Trying: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env
                )
                
                if result.returncode == 0:
                    print(f"✅ SUCCESS with: {cmd}")
                    print(f"   Output: {result.stdout[:200]}...")
                    
                    if verify_browser_installation():
                        print("✅ Browser installation verified!")
                        return True
                    else:
                        print("⚠️ Installation completed but verification failed")
                        continue
                else:
                    print(f"❌ FAILED: {cmd}")
                    print(f"   Error: {result.stderr[:200]}...")
                    
            except subprocess.TimeoutExpired:
                print(f"⏱️ TIMEOUT: {cmd}")
            except Exception as e:
                print(f"💥 ERROR: {cmd} - {str(e)}")
        
        print("❌ All installation methods failed")
        return False
        
    except Exception as e:
        print(f"💥 Critical error in browser installation: {e}")
        return False

def verify_browser_installation():
    """Verify browser installation with multiple checks"""
    try:
        browser_path = "/tmp/pw-browsers"
        
        if not os.path.exists(browser_path):
            print("❌ Browser directory doesn't exist")
            return False
        
        # Check for browser directories
        browser_found = False
        executable_found = False
        
        for item in os.listdir(browser_path):
            item_path = os.path.join(browser_path, item)
            if os.path.isdir(item_path) and ("chromium" in item.lower() or "chrome" in item.lower()):
                browser_found = True
                print(f"✅ Found browser directory: {item}")
                
                # Check for executables
                possible_executables = [
                    os.path.join(item_path, "chrome-linux", "chrome"),
                    os.path.join(item_path, "chrome-linux", "headless_shell"),
                    os.path.join(item_path, "chromium-linux", "chrome"),
                    os.path.join(item_path, "chromium-linux", "headless_shell"),
                    os.path.join(item_path, "chromium"),
                    os.path.join(item_path, "chrome"),
                    os.path.join(item_path, "headless_shell")
                ]
                
                for executable in possible_executables:
                    if os.path.exists(executable):
                        executable_found = True
                        print(f"✅ Found executable: {executable}")
                        if os.access(executable, os.X_OK):
                            print(f"✅ Executable is runnable: {executable}")
                            return True
                        else:
                            print(f"⚠️ Executable not runnable: {executable}")
        
        if browser_found and not executable_found:
            print("⚠️ Browser directory found but no executables")
        elif not browser_found:
            print("❌ No browser directories found")
        
        return False
        
    except Exception as e:
        print(f"❌ Error verifying browser installation: {e}")
        return False

def install_browsers_blocking():
    """Install browsers in blocking mode during startup"""
    global browser_installation_state
    
    print("🚀 Starting browser installation check...")
    
    if verify_browser_installation():
        browser_installation_state["is_installed"] = True
        print("✅ Browsers already installed and verified!")
        return True
    
    browser_installation_state["installation_in_progress"] = True
    browser_installation_state["installation_attempted"] = True
    
    print("🔫 Browsers not found. Starting installation...")
    
    try:
        success = install_with_python_module()
        
        if success:
            browser_installation_state["installation_in_progress"] = False
            browser_installation_state["is_installed"] = True
            print("🎉 Browser installation completed successfully!")
            return True
        
        success = force_install_browsers()
        
        if success:
            browser_installation_state["installation_in_progress"] = False
            browser_installation_state["is_installed"] = True
            print("🎉 Browser installation completed successfully!")
            return True
        
        success = install_with_script()
        
        if success:
            browser_installation_state["installation_in_progress"] = False
            browser_installation_state["is_installed"] = True
            print("🎉 Browser installation completed successfully!")
            return True
        
    except Exception as e:
        print(f"💥 Error during installation strategies: {e}")
    
    error_msg = "Failed to install Playwright browsers after trying all strategies"
    browser_installation_state["installation_in_progress"] = False
    browser_installation_state["is_installed"] = False
    browser_installation_state["installation_error"] = error_msg
    print(f"❌ {error_msg}")
    return False

def install_with_python_module():
    """Install browsers using the current Python executable"""
    try:
        env = os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
        env['PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'] = '0'
        
        os.makedirs("/tmp/pw-browsers", exist_ok=True)
        
        commands = [
            f"{sys.executable} -m playwright install chromium --with-deps",
            f"{sys.executable} -m playwright install chromium"
        ]
        
        for cmd in commands:
            try:
                print(f"🔫 Trying: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env
                )
                
                if result.returncode == 0:
                    print(f"✅ SUCCESS with: {cmd}")
                    if verify_browser_installation():
                        return True
                else:
                    print(f"❌ FAILED: {cmd}")
                    print(f"   Error: {result.stderr[:200]}...")
                    
            except Exception as e:
                print(f"💥 ERROR: {cmd} - {str(e)}")
        
        return False
        
    except Exception as e:
        print(f"💥 Critical error in python module installation: {e}")
        return False

def install_with_script():
    """Install browsers using the dedicated installation script"""
    try:
        result = subprocess.run(
            [sys.executable, "/app/install_playwright.py"],
            capture_output=True,
            text=True,
            timeout=600
        )
        
        if result.returncode == 0:
            print("✅ Installation script completed successfully")
            return verify_browser_installation()
        else:
            print(f"❌ Installation script failed: {result.stderr}")
            return False
            
    except Exception as e:
        print(f"💥 Error running installation script: {e}")
        return False

# Install browsers during startup
print("=" * 60)
print("MCQ SCRAPER - ULTRA-ROBUST VERSION")
print("=" * 60)

try:
    install_success = install_browsers_blocking()
except Exception as e:
    print(f"🚨 CRITICAL ERROR during browser installation: {e}")
    install_success = False

if not install_success:
    print("🚨 CRITICAL: Browser installation failed!")
    print("🚨 App will start but scraping functionality may be limited")
    browser_installation_state["is_installed"] = False
    browser_installation_state["installation_error"] = "Browser installation failed during startup"
else:
    print("✅ Browser installation successful - Ultra-Robust App ready!")

print("=" * 60)

# Graceful shutdown handling
def handle_shutdown(signum, frame):
    """Handle graceful shutdown"""
    print("🔫 Received shutdown signal, cleaning up...")
    asyncio.create_task(browser_pool.close())
    persistent_storage.save_jobs()
    print("✅ Cleanup completed")

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)
atexit.register(lambda: persistent_storage.save_jobs())

app = FastAPI(title="Ultra-Robust MCQ Scraper", version="3.0.0")

# Enhanced CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key Pool Manager
class APIKeyManager:
    def __init__(self):
        api_key_pool = os.getenv("API_KEY_POOL", "")
        self.api_keys = [key.strip() for key in api_key_pool.split(",") if key.strip()]
        self.current_key_index = 0
        self.exhausted_keys = set()
        
        if not self.api_keys:
            raise ValueError("No API keys found in environment")
        
        print(f"🔐 Initialized API Key Manager with {len(self.api_keys)} keys")
    
    def get_current_key(self) -> str:
        return self.api_keys[self.current_key_index]
    
    def rotate_key(self) -> Optional[str]:
        current_key = self.api_keys[self.current_key_index]
        self.exhausted_keys.add(current_key)
        print(f"⚠️ Key exhausted: {current_key[:20]}...")
        
        for i in range(len(self.api_keys)):
            key = self.api_keys[i]
            if key not in self.exhausted_keys:
                self.current_key_index = i
                print(f"🔄 Rotated to key: {key[:20]}...")
                return key
        
        print("❌ All API keys exhausted!")
        return None
    
    def is_quota_error(self, error_message: str) -> bool:
        quota_indicators = [
            "quota exceeded", "quotaExceeded", "rateLimitExceeded",
            "userRateLimitExceeded", "dailyLimitExceeded", "Too Many Requests"
        ]
        return any(indicator.lower() in error_message.lower() for indicator in quota_indicators)
    
    def get_remaining_keys(self) -> int:
        return len(self.api_keys) - len(self.exhausted_keys)

# Initialize API Key Manager
api_key_manager = APIKeyManager()

# Search Engine ID
SEARCH_ENGINE_ID = os.getenv("SEARCH_ENGINE_ID", "2701a7d64a00d47fd")

# Generated PDFs storage
generated_pdfs = {}

class SearchRequest(BaseModel):
    topic: str
    exam_type: str = "SSC"
    pdf_format: str = "text"

class MCQData(BaseModel):
    question: str
    options: List[str]
    answer: str
    exam_source_heading: str = ""
    exam_source_title: str = ""
    is_relevant: bool = True

class JobStatus(BaseModel):
    job_id: str
    status: str
    progress: str
    total_links: Optional[int] = 0
    processed_links: Optional[int] = 0
    mcqs_found: Optional[int] = 0
    pdf_url: Optional[str] = None

def update_job_progress(job_id: str, status: str, progress: str, **kwargs):
    """Update job progress using persistent storage with unicode fix"""
    try:
        # Clean unicode characters from progress messages
        clean_progress = progress.encode('utf-8', errors='ignore').decode('utf-8')
        
        # Replace emoji characters with text equivalents for better compatibility
        emoji_replacements = {
            '🚀': '[STARTING]',
            '📊': '[STATUS]',
            '✅': '[SUCCESS]',
            '❌': '[ERROR]',
            '⏱️': '[TIMEOUT]',
            '⚠️': '[WARNING]',
            '🎯': '[PROCESSING]',
            '📸': '[SCREENSHOT]',
            '🖼️': '[IMAGE]',
            '📄': '[PDF]',
            '🔄': '[RETRY]',
            '📍': '[FOUND]',
            '📏': '[DIMENSIONS]',
            '🔍': '[SEARCH]',
            '🌐': '[WEB]',
            '⭐': '[COMPLETE]',
            '🎉': '[DONE]',
            '💡': '[INFO]',
            '🔧': '[PROCESSING]',
            '🚩': '[FLAG]',
            '📈': '[PROGRESS]',
            '🎪': '[MCQ]',
            '💾': '[SAVED]'
        }
        
        for emoji, replacement in emoji_replacements.items():
            clean_progress = clean_progress.replace(emoji, replacement)
        
        persistent_storage.update_job(job_id, status, clean_progress, **kwargs)
    except Exception as e:
        print(f"[ERROR] Error updating job progress: {e}")

def clean_unwanted_text(text: str) -> str:
    """Remove unwanted text strings from scraped content"""
    unwanted_strings = [
        "Download Solution PDF", "Download PDF", "Attempt Online",
        "View all BPSC Exam Papers >", "View all SSC Exam Papers >",
        "View all BPSC Exam Papers", "View all SSC Exam Papers"
    ]
    
    cleaned_text = text
    for unwanted in unwanted_strings:
        cleaned_text = cleaned_text.replace(unwanted, "")
    
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()
    return cleaned_text

def clean_text_for_pdf(text: str) -> str:
    """Clean text for PDF generation"""
    if not text:
        return ""
    
    cleaned = re.sub(r'\s+', ' ', text).strip()
    
    unwanted_patterns = [
        r'Download\s+Solution\s+PDF', r'Download\s+PDF', r'Attempt\s+Online',
        r'View\s+all\s+\w+\s+Exam\s+Papers\s*>?'
    ]
    
    for pattern in unwanted_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    
    return cleaned.strip()

async def capture_page_screenshot_ultra_robust(page, url: str, topic: str) -> Optional[bytes]:
    """
    ENHANCED MCQ screenshot capture - Precise cropping to capture the exact format 
    shown in user's 2nd image: focused on question, options, and detailed solution only
    with improved dimensions for better visual quality
    """
    try:
        print(f"📸 Capturing precise MCQ screenshot for URL: {url}")
        
        # Navigate with optimized timeout
        try:
            await asyncio.wait_for(
                page.goto(url, wait_until="domcontentloaded", timeout=12000),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            print(f"⏱️ Navigation timeout for {url}")
            return None
        
        # Minimal wait for page settling
        await page.wait_for_timeout(800)
        
        # Set optimal viewport specifically for MCQ content (similar to user's 2nd image dimensions)
        await page.set_viewport_size({"width": 1200, "height": 800})  # Adjusted for better MCQ capture
        await page.wait_for_timeout(200)
        
        # PRECISE MCQ element detection - targeting exact components shown in user's 2nd image
        mcq_elements = {}
        priority_selectors = [
            # Question body - the main question text
            ('h1.questionBody.tag-h1, div.questionBody, h1, h2, .question-text', 'question'),
            
            # Options - the multiple choice options (A, B, C, D)
            ('li.option, .option, div[class*="option"], ol li, ul li', 'options'),
            
            # Answer section - where correct answer is displayed
            ('div[class*="answer"], .answer-section, .correct-answer, h2:contains("Answer")', 'answer_section'),
            
            # Detailed solution - the comprehensive explanation (critical for complete coverage)
            ('div[class*="detailed-solution"], div[class*="solution"], .solution-section, h2:contains("Detailed Solution"), .key-points', 'detailed_solution'),
            
            # Exam info - source information
            ('div.pyp-heading, .exam-info, .paper-info', 'exam_info')
        ]
        
        valid_elements = []
        
        # Enhanced element detection focusing on core MCQ components
        for selector, element_type in priority_selectors:
            try:
                if element_type == 'options':
                    elements = await page.query_selector_all(selector)
                    if elements and len(elements) >= 2:  # Must have at least 2 options
                        valid_elements.extend(elements)
                        mcq_elements[element_type] = elements
                        print(f"✅ Found {len(elements)} options")
                else:
                    # Try each selector variation
                    for single_selector in selector.split(', '):
                        element = await page.query_selector(single_selector.strip())
                        if element:
                            valid_elements.append(element)
                            mcq_elements[element_type] = element
                            print(f"✅ Found {element_type} element with selector: {single_selector.strip()}")
                            break  # Found one, move to next type
            except Exception as e:
                print(f"⚠️ Error finding {element_type}: {e}")
                continue
        
        # Validation - ensure we have essential MCQ components
        has_essential_elements = (
            'question' in mcq_elements and 
            'options' in mcq_elements
        )
        
        if not has_essential_elements:
            print(f"❌ Missing essential MCQ components on {url}")
            return None
        
        print(f"✅ Found complete MCQ structure - proceeding with precision screenshot")
        
        # Scroll to ensure all content is visible and positioned optimally
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        
        # Calculate precise bounding region that matches user's 2nd image format
        element_boxes = []
        for element in valid_elements:
            try:
                box = await element.bounding_box()
                if box and box['width'] > 10 and box['height'] > 10:
                    element_boxes.append(box)
                    print(f"📏 Element box: x={box['x']:.0f}, y={box['y']:.0f}, w={box['width']:.0f}, h={box['height']:.0f}")
            except:
                continue
        
        if not element_boxes:
            print(f"❌ Could not get element boundaries for {url}")
            return None
        
        # Calculate tight bounding box around complete MCQ content
        min_x = min(box['x'] for box in element_boxes)
        min_y = min(box['y'] for box in element_boxes) 
        max_x = max(box['x'] + box['width'] for box in element_boxes)
        max_y = max(box['y'] + box['height'] for box in element_boxes)
        
        print(f"📍 Content bounds: x={min_x:.0f}-{max_x:.0f}, y={min_y:.0f}-{max_y:.0f}")
        
        # PRECISION MARGINS - optimized to match user's 2nd image cropping style
        content_width = max_x - min_x
        content_height = max_y - min_y
        
        # Refined margins for precise cropping (smaller, more focused like user's 2nd image)
        horizontal_margin = min(60, content_width * 0.08)   # Reduced to 8% or max 60px for tighter crop
        vertical_margin = min(80, content_height * 0.10)    # Reduced to 10% or max 80px for better focus
        
        # Final precise screenshot region matching user's desired format
        screenshot_x = max(0, int(min_x - horizontal_margin))
        screenshot_y = max(0, int(min_y - vertical_margin))
        screenshot_width = min(1200, int(max_x - screenshot_x + horizontal_margin))
        screenshot_height = int(max_y - screenshot_y + vertical_margin)
        
        # Limit screenshot height for optimal display (matching user's 2nd image proportions)
        max_screenshot_height = 4000  # Reduced from 6000 for better proportion
        if screenshot_height > max_screenshot_height:
            print(f"⚠️ Screenshot height {screenshot_height}px exceeds maximum, capping at {max_screenshot_height}px")
            screenshot_height = max_screenshot_height
        
        screenshot_region = {
            "x": screenshot_x,
            "y": screenshot_y,
            "width": screenshot_width,
            "height": screenshot_height
        }
        
        print(f"🎯 PRECISE MCQ region (matching user's format): x={screenshot_x}, y={screenshot_y}, w={screenshot_width}, h={screenshot_height}")
        
        # Position page optimally for screenshot
        scroll_y = max(0, screenshot_y - 50)  # Reduced scroll offset for better positioning
        await page.evaluate(f"window.scrollTo(0, {scroll_y})")
        await page.wait_for_timeout(300)
        
        # Capture high-quality screenshot with precise dimensions
        try:
            screenshot = await asyncio.wait_for(
                page.screenshot(
                    clip=screenshot_region,
                    type="png"
                ),
                timeout=10.0
            )
            
            print(f"✅ PRECISE MCQ screenshot captured (user's format): {screenshot_width}x{screenshot_height}px")
            return screenshot
            
        except asyncio.TimeoutError:
            print(f"⏱️ Screenshot timeout for {url}")
            return None
        
    except Exception as e:
        print(f"❌ Error capturing precise screenshot for {url}: {str(e)}")
        return None

async def scrape_testbook_page_with_screenshot_ultra_robust(context: BrowserContext, url: str, topic: str) -> Optional[dict]:
    """
    Optimized screenshot scraping with faster processing and smart element detection
    """
    page = None
    try:
        print(f"🔍 Processing URL (optimized): {url}")
        
        # Fast page creation
        try:
            page = await asyncio.wait_for(context.new_page(), timeout=6.0)
        except asyncio.TimeoutError:
            print(f"⏱️ Page creation timeout for {url}")
            return None
        
        # Single navigation attempt with smart timeout
        try:
            await asyncio.wait_for(
                page.goto(url, wait_until="domcontentloaded", timeout=12000),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            print(f"⏱️ Navigation timeout for {url}")
            return None
        
        # Minimal page settle time
        await page.wait_for_timeout(500)
        
        # Quick relevance check - combined with question extraction
        question_text = ""
        try:
            # Try both selectors at once
            question_element = await page.query_selector('h1.questionBody.tag-h1, div.questionBody')
            
            if question_element:
                question_text = await question_element.inner_text()
                question_text = clean_unwanted_text(question_text)
            else:
                print(f"❌ No question element found on {url}")
                return None
                
        except Exception as e:
            print(f"⚠️ Error extracting question from {url}: {e}")
            return None
        
        # Fast relevance check
        if not is_mcq_relevant(question_text, topic):
            print(f"❌ MCQ not relevant for topic '{topic}' on {url}")
            return None
        
        print(f"✅ MCQ relevant for topic '{topic}'")
        
        # Optimized screenshot capture with improved precision
        screenshot = await capture_page_screenshot_ultra_robust(page, url, topic)
        
        if not screenshot:
            print(f"❌ Failed to capture screenshot for {url}")
            return None
        
        return {
            "url": url,
            "screenshot": screenshot,
            "is_relevant": True
        }
        
    except Exception as e:
        print(f"❌ Error processing {url}: {str(e)}")
        return None
    finally:
        if page:
            try:
                await asyncio.wait_for(page.close(), timeout=2.0)
            except:
                pass

def is_mcq_relevant(question_text: str, search_topic: str) -> bool:
    """Enhanced relevance checking"""
    if not question_text or not search_topic:
        return False
    
    question_lower = question_text.lower()
    topic_lower = search_topic.lower()
    
    # Enhanced topic matching
    topic_variations = [topic_lower]
    
    # Extended topic stems
    topic_stems = {
        'biology': ['biological', 'bio', 'organism', 'living', 'life', 'cell', 'plant', 'animal', 'species', 'photosynthesis', 'respiration', 'DNA', 'gene'],
        'physics': ['physical', 'force', 'energy', 'motion', 'matter', 'quantum', 'wave', 'particle', 'newton', 'gravity', 'electricity', 'magnetism'],
        'chemistry': ['chemical', 'reaction', 'compound', 'element', 'molecule', 'atom', 'bond', 'formula', 'water', 'oxygen', 'carbon', 'acid'],
        'heart': ['cardiac', 'cardiovascular', 'circulation', 'blood', 'pulse', 'artery', 'vein', 'pressure', 'organ', 'pump'],
        'mathematics': ['mathematical', 'math', 'equation', 'number', 'calculation', 'formula', 'solve', 'calculate', 'area', 'radius', 'circle', 'triangle'],
        'history': ['historical', 'past', 'ancient', 'period', 'era', 'dynasty', 'empire', 'civilization', 'war', 'battle', 'year', 'century'],
        'geography': ['geographical', 'location', 'place', 'region', 'area', 'continent', 'country', 'climate', 'capital', 'city', 'ocean', 'river', 'mountain'],
        'economics': ['economic', 'economy', 'market', 'trade', 'finance', 'gdp', 'inflation', 'banking', 'money', 'currency', 'investment'],
        'politics': ['political', 'government', 'policy', 'administration', 'governance', 'democracy', 'election', 'president', 'minister', 'parliament'],
        'computer': ['computing', 'software', 'hardware', 'algorithm', 'programming', 'digital', 'binary', 'data', 'internet', 'technology'],
        'science': ['scientific', 'research', 'theory', 'experiment', 'hypothesis', 'discovery', 'planet', 'solar', 'universe', 'nature'],
        'english': ['grammar', 'vocabulary', 'literature', 'language', 'sentence', 'word', 'comprehension', 'reading', 'writing'],
        'reasoning': ['logical', 'logic', 'puzzle', 'pattern', 'sequence', 'analogy', 'verbal', 'analytical', 'solve', 'problem'],
        'cell': ['cellular', 'membrane', 'nucleus', 'mitosis', 'meiosis', 'organelle', 'cytoplasm', 'ribosome', 'mitochondria', 'chromosome'],
        'mitosis': ['cell', 'division', 'chromosome', 'spindle', 'kinetochore', 'centromere', 'anaphase', 'metaphase', 'prophase', 'telophase'],
        'excel': ['spreadsheet', 'microsoft', 'worksheet', 'formula', 'function', 'chart', 'pivot', 'vlookup', 'hlookup', 'macro']
    }
    
    if topic_lower in topic_stems:
        topic_variations.extend(topic_stems[topic_lower])
    
    # Add individual words
    topic_words = topic_lower.split()
    for word in topic_words:
        if len(word) > 3:
            topic_variations.append(word)
    
    # Add word stems
    if len(topic_lower) > 4:
        root_word = topic_lower
        suffixes = ['ical', 'ing', 'ed', 'er', 'est', 'ly', 'tion', 'sion', 'ness', 'ment', 'ogy', 'ics']
        for suffix in suffixes:
            if root_word.endswith(suffix) and len(root_word) > len(suffix) + 2:
                root_word = root_word[:-len(suffix)]
                topic_variations.append(root_word)
                break
    
    # Remove duplicates and sort
    topic_variations = sorted(list(set(topic_variations)), key=len, reverse=True)
    
    # Check for matches
    for variation in topic_variations:
        if len(variation) > 2 and variation in question_lower:
            return True
    
    return False

async def search_google_custom(topic: str, exam_type: str = "SSC") -> List[str]:
    """Search Google Custom Search API with enhanced error handling"""
    if exam_type.upper() == "BPSC":
        query = f'{topic} Testbook [Solved] "This question was previously asked in" ("BPSC" OR "Bihar Public Service Commission" OR "BPSC Combined" OR "BPSC Prelims") '
    else:
        query = f'{topic} Testbook [Solved] "This question was previously asked in" "SSC" '
    
    base_url = "https://www.googleapis.com/customsearch/v1"
    headers = {
        "Referer": "https://testbook.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    all_testbook_links = []
    start_index = 1
    max_results = 40  # Reduced for better performance
    
    try:
        while start_index <= max_results:
            current_key = api_key_manager.get_current_key()
            
            params = {
                "key": current_key,
                "cx": SEARCH_ENGINE_ID,
                "q": query,
                "num": 10,
                "start": start_index
            }
            
            print(f"🔍 Fetching results {start_index}-{start_index+9} for topic: {topic}")
            print(f"🔐 Using key: {current_key[:20]}... (Remaining: {api_key_manager.get_remaining_keys()})")
            
            response = requests.get(base_url, params=params, headers=headers)
            
            if response.status_code == 429 or (response.status_code == 403 and "quota" in response.text.lower()):
                print(f"⚠️ Quota exceeded for current key. Attempting rotation...")
                
                next_key = api_key_manager.rotate_key()
                if next_key is None:
                    print("❌ All API keys exhausted!")
                    raise Exception("All Servers are exhausted due to intense use")
                
                continue
            
            response.raise_for_status()
            data = response.json()
            
            if "items" not in data or len(data["items"]) == 0:
                print(f"No more results found after {start_index-1} results")
                break
            
            batch_links = []
            for item in data["items"]:
                link = item.get("link", "")
                if "testbook.com" in link:
                    batch_links.append(link)
            
            all_testbook_links.extend(batch_links)
            print(f"✅ Found {len(batch_links)} Testbook links in this batch. Total so far: {len(all_testbook_links)}")
            
            if len(data["items"]) < 10:
                print(f"Reached end of results with {len(data['items'])} items in last batch")
                break
            
            start_index += 10
            await asyncio.sleep(0.5)
        
        print(f"✅ Total Testbook links found: {len(all_testbook_links)}")
        return all_testbook_links
        
    except Exception as e:
        print(f"❌ Error searching Google: {e}")
        if "All Servers are exhausted due to intense use" in str(e):
            raise e
        return []

async def scrape_mcq_content_with_page_ultra_robust(page, url: str, search_topic: str) -> Optional[MCQData]:
    """Ultra-robust MCQ content scraping"""
    try:
        # Navigate with timeout
        try:
            await asyncio.wait_for(
                page.goto(url, wait_until='domcontentloaded', timeout=12000),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            print(f"⏱️ Navigation timeout for {url}")
            return None
        
        await page.wait_for_timeout(800)
        
        # Extract question with timeout
        question = ""
        try:
            question_selectors = ['h1.questionBody.tag-h1', 'div.questionBody']
            for selector in question_selectors:
                try:
                    element = await asyncio.wait_for(page.query_selector(selector), timeout=3.0)
                    if element:
                        question = await asyncio.wait_for(element.inner_text(), timeout=3.0)
                        break
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            print(f"⚠️ Error extracting question from {url}: {e}")
            return None
        
        if not question:
            print(f"❌ No question found on {url}")
            return None
        
        question = clean_unwanted_text(question)
        
        # Check relevance
        if not is_mcq_relevant(question, search_topic):
            print(f"❌ MCQ not relevant for topic '{search_topic}'")
            return None
        
        print(f"✅ MCQ relevant - topic '{search_topic}' found in question body")
        
        # Extract other elements
        options = []
        answer = ""
        exam_source_heading = ""
        exam_source_title = ""
        
        try:
            # Options
            option_elements = await asyncio.wait_for(page.query_selector_all('li.option'), timeout=3.0)
            if option_elements:
                for option_elem in option_elements:
                    try:
                        option_text = await asyncio.wait_for(option_elem.inner_text(), timeout=2.0)
                        options.append(clean_unwanted_text(option_text.strip()))
                    except asyncio.TimeoutError:
                        continue
            
            # Answer
            answer_element = await asyncio.wait_for(page.query_selector('.solution'), timeout=2.0)
            if answer_element:
                answer = await asyncio.wait_for(answer_element.inner_text(), timeout=2.0)
                answer = clean_unwanted_text(answer)
            
            # Exam source
            try:
                exam_heading_element = await asyncio.wait_for(page.query_selector('div.pyp-heading'), timeout=1.0)
                if exam_heading_element:
                    exam_source_heading = await asyncio.wait_for(exam_heading_element.inner_text(), timeout=1.0)
                    exam_source_heading = clean_unwanted_text(exam_source_heading)
                
                exam_title_element = await asyncio.wait_for(page.query_selector('div.pyp-title.line-ellipsis'), timeout=1.0)
                if exam_title_element:
                    exam_source_title = await asyncio.wait_for(exam_title_element.inner_text(), timeout=1.0)
                    exam_source_title = clean_unwanted_text(exam_source_title)
            except asyncio.TimeoutError:
                pass
                
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout extracting elements from {url}")
        
        # Return MCQ data
        if question and (options or answer):
            return MCQData(
                question=question.strip(),
                options=options,
                answer=answer.strip(),
                exam_source_heading=exam_source_heading.strip(),
                exam_source_title=exam_source_title.strip(),
                is_relevant=True
            )
        
        return None
        
    except Exception as e:
        print(f"❌ Error scraping {url}: {e}")
        return None

async def scrape_mcq_content_ultra_robust(url: str, search_topic: str) -> Optional[MCQData]:
    """Ultra-robust MCQ scraping with maximum error handling"""
    context = None
    page = None
    max_attempts = 3
    
    for attempt in range(max_attempts):
        try:
            print(f"🔍 Scraping attempt {attempt + 1} for {url}")
            
            # Get context with retries
            try:
                context = await browser_pool.get_context()
            except Exception as e:
                print(f"⚠️ Failed to get browser context (attempt {attempt + 1}): {e}")
                if attempt == max_attempts - 1:
                    return None
                await asyncio.sleep(3)
                continue
            
            # Create page with timeout
            try:
                page = await asyncio.wait_for(context.new_page(), timeout=8.0)
            except asyncio.TimeoutError:
                print(f"⏱️ Page creation timeout (attempt {attempt + 1})")
                if context:
                    await context.close()
                if attempt == max_attempts - 1:
                    return None
                await asyncio.sleep(3)
                continue
            
            # Scrape content
            result = await scrape_mcq_content_with_page_ultra_robust(page, url, search_topic)
            return result
            
        except Exception as e:
            print(f"❌ Error in scraping attempt {attempt + 1} for {url}: {e}")
            if attempt == max_attempts - 1:
                return None
            await asyncio.sleep(3)
        finally:
            if page:
                try:
                    await asyncio.wait_for(page.close(), timeout=3.0)
                except:
                    pass
            if context:
                try:
                    await asyncio.wait_for(context.close(), timeout=3.0)
                except:
                    pass
    
    return None

# CUSTOM VISUAL ELEMENTS - Beautiful Graphics
class GradientHeader(Flowable):
    """Custom gradient header with beautiful visual design"""
    def __init__(self, width, height, title, subtitle=""):
        Flowable.__init__(self)
        self.width = width
        self.height = height
        self.title = title
        self.subtitle = subtitle
        
    def draw(self):
        canvas = self.canv
        
        # Define colors (these will be available when the class is used)
        gradient_start = HexColor('#667eea')     # Purple-Blue
        white = HexColor('#ffffff')
        
        # Gradient background rectangle
        canvas.setFillColor(gradient_start)
        canvas.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        
        # Overlay with subtle gradient effect
        canvas.setFillColorRGB(0.4, 0.47, 0.91, alpha=0.8)
        canvas.rect(0, 0, self.width, self.height * 0.6, fill=1, stroke=0)
        
        # Decorative circles
        canvas.setFillColor(white)
        canvas.setFillColorRGB(1, 1, 1, alpha=0.2)
        canvas.circle(self.width - 30, self.height - 20, 15, fill=1)
        canvas.circle(self.width - 70, self.height - 35, 10, fill=1)
        canvas.circle(self.width - 110, self.height - 15, 8, fill=1)
        
        # Title text
        canvas.setFillColor(white)
        canvas.setFont("Helvetica-Bold", 20)
        text_width = canvas.stringWidth(self.title, "Helvetica-Bold", 20)
        canvas.drawString((self.width - text_width) / 2, self.height - 35, self.title)
        
        # Subtitle if provided
        if self.subtitle:
            canvas.setFont("Helvetica", 12)
            sub_width = canvas.stringWidth(self.subtitle, "Helvetica", 12)
            canvas.drawString((self.width - sub_width) / 2, self.height - 55, self.subtitle)

class DecorativeSeparator(Flowable):
    """Beautiful decorative separator line"""
    def __init__(self, width, height=0.1*inch):
        Flowable.__init__(self)
        self.width = width
        self.height = height
        
    def draw(self):
        canvas = self.canv
        y = self.height / 2
        
        # Define colors
        primary_color = HexColor('#1a365d')      # Deep Navy Blue
        accent_color = HexColor('#38b2ac')       # Teal
        secondary_color = HexColor('#2b6cb0')    # Medium Blue
        
        # Main gradient line
        canvas.setStrokeColor(primary_color)
        canvas.setLineWidth(3)
        canvas.line(0, y, self.width * 0.3, y)
        
        canvas.setStrokeColor(accent_color)  
        canvas.line(self.width * 0.3, y, self.width * 0.7, y)
        
        canvas.setStrokeColor(secondary_color)
        canvas.line(self.width * 0.7, y, self.width, y)
        
        # Decorative diamonds
        canvas.setFillColor(accent_color)
        diamond_size = 4
        for x_pos in [self.width * 0.2, self.width * 0.5, self.width * 0.8]:
            canvas.circle(x_pos, y, diamond_size, fill=1)

def generate_pdf(mcqs: List[MCQData], topic: str, job_id: str, relevant_mcqs: int, irrelevant_mcqs: int, total_links: int) -> str:
    """Generate BEAUTIFUL PROFESSIONAL PDF with enhanced design, graphics, and premium styling"""
    try:
        pdf_dir = get_pdf_directory()
        
        filename = f"Testbook_MCQs_{topic.replace(' ', '_')}_{job_id}.pdf"
        filepath = pdf_dir / filename
        
        doc = SimpleDocTemplate(str(filepath), pagesize=A4, 
                              topMargin=0.6*inch, bottomMargin=0.6*inch,
                              leftMargin=0.6*inch, rightMargin=0.6*inch)
        
        styles = getSampleStyleSheet()
        
        # 🎨 PREMIUM Color Palette - Professional & Eye-catching
        primary_color = HexColor('#1a365d')      # Deep Navy Blue
        secondary_color = HexColor('#2b6cb0')    # Medium Blue
        accent_color = HexColor('#38b2ac')       # Teal
        highlight_color = HexColor('#ed8936')    # Orange
        success_color = HexColor('#48bb78')      # Green
        light_color = HexColor('#f7fafc')        # Light Gray
        dark_color = HexColor('#2d3748')         # Dark Gray
        
        story = []
        
        # 🌟 STUNNING HEADER with Custom Graphics
        story.append(GradientHeader(doc.width, 1.2*inch, 
                     f"🎯 TESTBOOK MCQ COLLECTION", 
                     f"Topic: {topic.upper()} | Total Questions: {len(mcqs)}"))
        
        story.append(Spacer(1, 0.3*inch))
        
        # 📊 PREMIUM Statistics Section with Beautiful Styling
        stats_style = ParagraphStyle('StatsStyle', parent=styles['Normal'],
            fontSize=13, textColor=primary_color, alignment=TA_CENTER,
            fontName='Helvetica-Bold', spaceAfter=10, spaceBefore=10,
            borderWidth=2, borderColor=accent_color, borderPadding=15,
            backColor=light_color, borderRadius=10)
        
        stats_content = f"""
        <b>📈 COLLECTION STATISTICS</b><br/><br/>
        🎪 <b>Total MCQs Found:</b> {len(mcqs)}<br/>
        ✅ <b>Relevant Questions:</b> {relevant_mcqs}<br/>
        ❌ <b>Filtered Out:</b> {irrelevant_mcqs}<br/>
        🌐 <b>Sources Processed:</b> {total_links}<br/>
        📅 <b>Generated On:</b> {datetime.now().strftime('%B %d, %Y at %I:%M %p')}
        """
        
        story.append(Paragraph(stats_content, stats_style))
        story.append(Spacer(1, 0.4*inch))
        
        # Replace simple separator with premium DecorativeSeparator
        story.append(DecorativeSeparator(doc.width, 0.15*inch))
        story.append(PageBreak())
        
        # 📝 ENHANCED MCQ CONTENT with Beautiful Styling
        for i, mcq in enumerate(mcqs, 1):
            # Professional Question header with graphics
            question_header_style = ParagraphStyle('QuestionHeader', parent=styles['Heading2'],
                fontSize=16, textColor=white, alignment=TA_CENTER,
                fontName='Helvetica-Bold', spaceAfter=12, spaceBefore=12,
                backColor=primary_color, borderPadding=10, borderRadius=8)
            
            story.append(Paragraph(f"🎯 QUESTION {i} OF {len(mcqs)} 🎯", question_header_style))
            story.append(Spacer(1, 0.15*inch))
            
            # FIXED: Use correct attribute from MCQData model
            # Changed from mcq.exam_info to mcq.exam_source_heading or mcq.exam_source_title
            exam_info = ""
            if mcq.exam_source_heading:
                exam_info = mcq.exam_source_heading
            elif mcq.exam_source_title:
                exam_info = mcq.exam_source_title
            else:
                exam_info = f"{topic} Practice Question"
                
            story.append(Paragraph(f"📋 <i>{exam_info}</i>", 
                ParagraphStyle('ExamInfo', parent=styles['Normal'], 
                    fontSize=11, textColor=secondary_color, alignment=TA_CENTER, 
                    fontName='Helvetica-Oblique', spaceAfter=15,
                    borderWidth=1, borderColor=accent_color, borderPadding=8, 
                    backColor=light_color, borderRadius=5)))
            
            story.append(Spacer(1, 0.1*inch))
            
            # Question with enhanced styling
            question_style = ParagraphStyle('QuestionStyle', parent=styles['Normal'],
                fontSize=12, textColor=dark_color, alignment=TA_LEFT,
                fontName='Helvetica', spaceAfter=15, spaceBefore=5,
                borderWidth=1, borderColor=primary_color, borderPadding=12,
                backColor=HexColor('#f8f9fa'), borderRadius=6,
                leftIndent=10, rightIndent=10)
            
            if mcq.question:
                question_text = mcq.question.replace('\n', '<br/>')
                story.append(Paragraph(f"❓ <b>QUESTION:</b><br/><br/>{question_text}", question_style))
            
            story.append(Spacer(1, 0.15*inch))
            
            # Beautiful Options with enhanced styling
            if mcq.options:
                option_style = ParagraphStyle('OptionStyle', parent=styles['Normal'],
                    fontSize=11, textColor=dark_color, alignment=TA_LEFT,
                    fontName='Helvetica', spaceAfter=8, spaceBefore=3,
                    leftIndent=20, rightIndent=10,
                    borderWidth=1, borderColor=accent_color, borderPadding=8,
                    backColor=HexColor('#ffffff'), borderRadius=4)
                
                # Options with colorful bullets
                option_letters = ['A', 'B', 'C', 'D', 'E', 'F']
                for j, option in enumerate(mcq.options[:6]):  # Limit to 6 options
                    letter = option_letters[j] if j < len(option_letters) else str(j+1)
                    clean_option = clean_text_for_pdf(option)
                    story.append(Paragraph(f"🔹 <b>{letter})</b> {clean_option}", option_style))
            
            story.append(Spacer(1, 0.2*inch))
            
            # Answer Section with premium styling
            if mcq.answer:
                answer_style = ParagraphStyle('AnswerStyle', parent=styles['Normal'],
                    fontSize=11, textColor=success_color, alignment=TA_LEFT,
                    fontName='Helvetica-Bold', spaceAfter=12, spaceBefore=8,
                    borderWidth=2, borderColor=success_color, borderPadding=12,
                    backColor=HexColor('#f0fff4'), borderRadius=6,
                    leftIndent=10, rightIndent=10)
                
                clean_answer = clean_text_for_pdf(mcq.answer)
                story.append(Paragraph(f"✅ <b>DETAILED SOLUTION:</b><br/><br/>{clean_answer}", answer_style))
            
            # Add elegant separator between questions (except for last question)
            if i < len(mcqs):
                story.append(Spacer(1, 0.3*inch))
                story.append(DecorativeSeparator(doc.width, 0.08*inch))
                story.append(PageBreak())
        
        # 🎨 PREMIUM FOOTER Section
        footer_style = ParagraphStyle('FooterStyle', parent=styles['Normal'],
            fontSize=10, textColor=primary_color, alignment=TA_CENTER,
            fontName='Helvetica-Oblique', spaceAfter=10, spaceBefore=20,
            borderWidth=1, borderColor=accent_color, borderPadding=15,
            backColor=light_color, borderRadius=8)
        
        footer_content = f"""
        <b>🏆 ULTRA-ROBUST MCQ SCRAPER</b><br/>
        Generated with precision and excellence | Topic: {topic}<br/>
        📊 {len(mcqs)} High-Quality Questions | 🎯 Relevant & Verified<br/>
        ⭐ Professional PDF Generation | {datetime.now().strftime('%Y')} Ultra-Robust Technology
        """
        
        story.append(Spacer(1, 0.4*inch))
        story.append(Paragraph(footer_content, footer_style))
        
        # Build the PDF with enhanced error handling
        doc.build(story)
        
        print(f"✅ BEAUTIFUL PDF generated successfully: {filename}")
        return filename
        
    except Exception as e:
        print(f"❌ Error generating PDF: {e}")
        raise

def generate_screenshot_pdf(screenshots: List[dict], topic: str, job_id: str) -> str:
    """Generate PDF with screenshots"""
    try:
        pdf_dir = get_pdf_directory()
        
        filename = f"Testbook_Screenshots_{topic.replace(' ', '_')}_{job_id}.pdf"
        filepath = pdf_dir / filename
        
        doc = SimpleDocTemplate(str(filepath), pagesize=A4, 
                              topMargin=0.5*inch, bottomMargin=0.5*inch,
                              leftMargin=0.5*inch, rightMargin=0.5*inch)
        
        styles = getSampleStyleSheet()
        story = []
        
        # Header
        header_style = ParagraphStyle('HeaderStyle', parent=styles['Title'],
            fontSize=18, textColor=HexColor('#1a365d'), alignment=TA_CENTER,
            fontName='Helvetica-Bold', spaceAfter=20)
        
        story.append(Paragraph(f"🎯 TESTBOOK MCQ SCREENSHOTS", header_style))
        story.append(Paragraph(f"Topic: {topic}", styles['Heading2']))
        story.append(Spacer(1, 0.3*inch))
        
        # Add screenshots
        from reportlab.lib.utils import ImageReader
        from io import BytesIO
        
        for i, screenshot_data in enumerate(screenshots, 1):
            # Question header
            story.append(Paragraph(f"Question {i} of {len(screenshots)}", styles['Heading3']))
            story.append(Spacer(1, 0.1*inch))
            
            # URL info
            story.append(Paragraph(f"Source: {screenshot_data['url']}", styles['Normal']))
            story.append(Spacer(1, 0.2*inch))
            
            # Screenshot
            try:
                img_stream = BytesIO(screenshot_data['screenshot'])
                img_reader = ImageReader(img_stream)
                
                # Calculate dimensions to fit page width
                img_width, img_height = img_reader.getSize()
                available_width = doc.width
                available_height = doc.height - 2*inch  # Leave space for headers
                
                # Scale image to fit
                if img_width > available_width:
                    scale_factor = available_width / img_width
                    img_width = available_width
                    img_height = img_height * scale_factor
                
                if img_height > available_height:
                    scale_factor = available_height / img_height
                    img_height = available_height
                    img_width = img_width * scale_factor
                
                from reportlab.platypus import Image
                story.append(Image(img_stream, width=img_width, height=img_height))
                
            except Exception as e:
                print(f"Error adding screenshot {i}: {e}")
                story.append(Paragraph(f"[Screenshot {i} could not be displayed]", styles['Normal']))
            
            # Add page break except for last screenshot
            if i < len(screenshots):
                story.append(PageBreak())
        
        # Build PDF
        doc.build(story)
        
        print(f"✅ Screenshot PDF generated successfully: {filename}")
        return filename
        
    except Exception as e:
        print(f"❌ Error generating screenshot PDF: {e}")
        raise

async def process_search_request_ultra_robust(request: SearchRequest, job_id: str):
    """Ultra-robust search processing with comprehensive error handling"""
    try:
        update_job_progress(job_id, "running", "🚀 Starting ultra-robust search process...")
        
        # Search for links
        update_job_progress(job_id, "running", f"🔍 Searching for {request.topic} MCQs...")
        testbook_links = await search_google_custom(request.topic, request.exam_type)
        
        if not testbook_links:
            update_job_progress(job_id, "completed", "❌ No links found", total_links=0, mcqs_found=0)
            return
        
        update_job_progress(job_id, "running", f"✅ Found {len(testbook_links)} links to process", 
                          total_links=len(testbook_links))
        
        # Process based on format
        if request.pdf_format == "screenshot":
            await process_screenshot_format(testbook_links, request.topic, job_id)
        else:
            await process_text_format(testbook_links, request.topic, job_id)
            
    except Exception as e:
        error_msg = f"Critical error in search processing: {str(e)}"
        print(f"❌ {error_msg}")
        update_job_progress(job_id, "failed", f"❌ {error_msg}")

async def process_screenshot_format(testbook_links: List[str], topic: str, job_id: str):
    """Process links for screenshot format PDF"""
    try:
        update_job_progress(job_id, "running", "📸 Processing screenshots...")
        
        screenshots = []
        processed = 0
        
        context = await browser_pool.get_context()
        
        for link in testbook_links:
            try:
                update_job_progress(job_id, "running", 
                                  f"📸 Processing screenshot {processed + 1}/{len(testbook_links)}",
                                  processed_links=processed + 1)
                
                result = await scrape_testbook_page_with_screenshot_ultra_robust(context, link, topic)
                
                if result and result.get('screenshot'):
                    screenshots.append(result)
                    print(f"✅ Screenshot captured for {link}")
                else:
                    print(f"❌ Failed to capture screenshot for {link}")
                
            except Exception as e:
                print(f"⚠️ Error processing {link}: {e}")
            
            processed += 1
            
            # Rate limiting
            await asyncio.sleep(1)
        
        await context.close()
        
        if screenshots:
            update_job_progress(job_id, "running", f"📄 Generating PDF with {len(screenshots)} screenshots...")
            
            filename = generate_screenshot_pdf(screenshots, topic, job_id)
            pdf_url = f"/download-pdf/{filename}"
            
            update_job_progress(job_id, "completed", 
                              f"✅ Screenshot PDF generated successfully!",
                              mcqs_found=len(screenshots), pdf_url=pdf_url)
        else:
            update_job_progress(job_id, "completed", "❌ No screenshots captured", mcqs_found=0)
            
    except Exception as e:
        error_msg = f"Error in screenshot processing: {str(e)}"
        print(f"❌ {error_msg}")
        update_job_progress(job_id, "failed", f"❌ {error_msg}")

async def process_text_format(testbook_links: List[str], topic: str, job_id: str):
    """Process links for text format PDF"""
    try:
        update_job_progress(job_id, "running", "📝 Processing text content...")
        
        all_mcqs = []
        processed = 0
        relevant_count = 0
        irrelevant_count = 0
        
        for link in testbook_links:
            try:
                update_job_progress(job_id, "running", 
                                  f"📝 Processing text {processed + 1}/{len(testbook_links)}",
                                  processed_links=processed + 1)
                
                mcq_data = await scrape_mcq_content_ultra_robust(link, topic)
                
                if mcq_data:
                    if mcq_data.is_relevant:
                        all_mcqs.append(mcq_data)
                        relevant_count += 1
                        print(f"✅ Relevant MCQ found: {link}")
                    else:
                        irrelevant_count += 1
                        print(f"⚠️ Irrelevant MCQ filtered: {link}")
                else:
                    irrelevant_count += 1
                    print(f"❌ No MCQ data found: {link}")
                
            except Exception as e:
                print(f"⚠️ Error processing {link}: {e}")
                irrelevant_count += 1
            
            processed += 1
            
            # Rate limiting
            await asyncio.sleep(1)
        
        if all_mcqs:
            update_job_progress(job_id, "running", 
                              f"📄 Generating PDF with {len(all_mcqs)} MCQs...",
                              mcqs_found=len(all_mcqs))
            
            filename = generate_pdf(all_mcqs, topic, job_id, relevant_count, irrelevant_count, len(testbook_links))
            pdf_url = f"/download-pdf/{filename}"
            
            update_job_progress(job_id, "completed", 
                              f"✅ Text PDF generated successfully!",
                              mcqs_found=len(all_mcqs), pdf_url=pdf_url)
        else:
            update_job_progress(job_id, "completed", "❌ No relevant MCQs found", mcqs_found=0)
            
    except Exception as e:
        error_msg = f"Error in text processing: {str(e)}"
        print(f"❌ {error_msg}")
        update_job_progress(job_id, "failed", f"❌ {error_msg}")

# API ENDPOINTS
@app.get("/")
async def root():
    """Root endpoint with enhanced status"""
    return {
        "message": "Ultra-Robust MCQ Scraper API",
        "version": "3.0.0",
        "status": "operational",
        "browser_status": browser_installation_state,
        "features": ["text_pdf", "screenshot_pdf", "ultra_robust_processing"]
    }

@app.post("/search")
async def search_mcqs(request: SearchRequest, background_tasks: BackgroundTasks):
    """Search for MCQs with ultra-robust processing"""
    try:
        # Generate unique job ID
        job_id = str(uuid.uuid4())[:8]
        
        # Initialize job
        update_job_progress(job_id, "starting", "🚀 Initializing search request...")
        
        # Add to background tasks
        background_tasks.add_task(process_search_request_ultra_robust, request, job_id)
        
        return {
            "job_id": job_id,
            "status": "started",
            "message": f"Search started for topic: {request.topic}",
            "check_status_url": f"/status/{job_id}"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error starting search: {str(e)}")

@app.get("/status/{job_id}")
async def get_job_status(job_id: str):
    """Get job status with comprehensive information"""
    try:
        job_data = persistent_storage.get_job(job_id)
        
        if not job_data:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return JobStatus(**job_data)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting job status: {str(e)}")

@app.get("/download-pdf/{filename}")
async def download_pdf(filename: str):
    """Download generated PDF"""
    try:
        pdf_dir = get_pdf_directory()
        filepath = pdf_dir / filename
        
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="PDF not found")
        
        return FileResponse(
            path=str(filepath),
            filename=filename,
            media_type='application/pdf'
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error downloading PDF: {str(e)}")

@app.get("/health")
async def health_check():
    """Enhanced health check"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "browser_installed": browser_installation_state["is_installed"],
        "active_jobs": len(persistent_storage.jobs),
        "version": "3.0.0"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
