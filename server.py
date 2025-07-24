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
                print(f"📂 Loaded {len(self.jobs)} jobs from persistent storage")
            else:
                self.jobs = {}
                print("📂 No persistent storage found, starting fresh")
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
                print(f"🗑️ Cleaned up {len(jobs_to_remove)} old jobs")
                
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
            print("🗑️ Cleared temporary files")
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
        
        print(f"🔑 Initialized API Key Manager with {len(self.api_keys)} keys")
    
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

def is_exam_type_relevant(exam_source_heading: str, exam_source_title: str, exam_type: str) -> bool:
    """Check if MCQ is relevant for the specified exam type based on exam source information"""
    if not exam_type:
        return True  # If no exam type specified, accept all
    
    exam_type = exam_type.upper()
    
    # Combine both exam source texts for checking
    combined_exam_source = f"{exam_source_heading} {exam_source_title}".upper()
    
    if exam_type == "SSC":
        # MCQ must contain "SSC" to be relevant for SSC exam type
        return "SSC" in combined_exam_source
    elif exam_type == "BPSC":
        # MCQ must contain "BPSC" to be relevant for BPSC exam type
        return "BPSC" in combined_exam_source
    
    return True  # Default to accepting if exam type not recognized

async def capture_page_screenshot_ultra_robust(page, url: str, topic: str) -> Optional[bytes]:
    """
    Optimized MCQ screenshot capture with 67% zoom and consistent central area cropping
    Captures question, options, and answer section as shown in user requirements
    """
    try:
        print(f"📸 Capturing focused MCQ screenshot with 67% zoom for URL: {url}")
        
        # Single fast navigation with reasonable timeout
        try:
            await asyncio.wait_for(
                page.goto(url, wait_until="domcontentloaded", timeout=12000),
                timeout=15.0
            )
        except asyncio.TimeoutError:
            print(f"⏱️ Navigation timeout for {url}")
            return None
        
        # Wait for page settling
        await page.wait_for_timeout(800)
        
        # Set viewport for optimal MCQ content display (keeping working resolution)
        await page.set_viewport_size({"width": 1200, "height": 800})  # REVERTED: Back to working resolution
        await page.wait_for_timeout(200)
        
        # CRITICAL: Set page zoom to 67% as requested
        await page.evaluate("document.body.style.zoom = '0.67'")
        await page.wait_for_timeout(500)  # Allow zoom to take effect
        
        # Scroll to ensure we can see the complete MCQ content including answer
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
        
        # Quick relevance check by looking for question content
        question_found = False
        try:
            question_element = await page.query_selector('h1.questionBody, div.questionBody, h1, h2, h3')
            if question_element:
                question_text = await question_element.inner_text()
                if question_text and len(question_text.strip()) > 10:
                    question_found = True
                    print(f"✅ Found question content")
        except Exception as e:
            print(f"⚠️ Error checking question content: {e}")
        
        if not question_found:
            print(f"❌ No valid question content found on {url}")
            return None
        
        # Get page dimensions after zoom
        page_height = await page.evaluate("document.body.scrollHeight")
        page_width = await page.evaluate("document.body.scrollWidth") 
        viewport_height = await page.evaluate("window.innerHeight")
        
        print(f"📏 Page dimensions with 67% zoom: {page_width}x{page_height}, viewport: {viewport_height}")
        
        # ENHANCED: Calculate central area cropping (optimized for perfect balance)
        # Based on 1200px viewport width, crop consistently from edges  
        crop_left = 100      # Crop less from left edge  
        crop_top = 100       # Crop from top edge
        crop_right = 430     # INCREASED: 80px more cropping from right edge (350 + 80)
        
        # Calculate screenshot dimensions for central MCQ area
        screenshot_x = crop_left
        screenshot_y = crop_top  
        screenshot_width = 1200 - crop_left - crop_right  # Central content width
        
        # ENHANCED: Calculate height to include question, options, AND answer section
        # Based on typical MCQ layout, this should capture the essential content
        base_height = 600    # Base height for question + options (working resolution)
        answer_section_height = 600  # INCREASED by 200px MORE: Additional height for answer section (was 400, now 600)
        screenshot_height = base_height + answer_section_height
        
        # Ensure we don't exceed reasonable limits
        max_height = 1400    # INCREASED: Allow for extra 200px MORE bottom padding (was 1200, now 1400)
        if screenshot_height > max_height:
            screenshot_height = max_height
            
        # Ensure screenshot region is within page bounds
        screenshot_width = min(screenshot_width, page_width - screenshot_x)
        screenshot_height = min(screenshot_height, page_height - screenshot_y)
        
        screenshot_region = {
            "x": screenshot_x,
            "y": screenshot_y, 
            "width": screenshot_width,
            "height": screenshot_height
        }
        
        print(f"🎯 MCQ screenshot region (67% zoom): x={screenshot_x}, y={screenshot_y}, w={screenshot_width}, h={screenshot_height}")
        
        # Capture the screenshot with maximum quality settings
        try:
            screenshot = await asyncio.wait_for(
                page.screenshot(
                    clip=screenshot_region,
                    type="png",
                    omit_background=False  # FIXED: Changed from omitBackground to omit_background
                    # NOTE: quality parameter removed as it's unsupported for PNG screenshots
                ),
                timeout=15.0  # INCREASED: More time for high quality capture
            )
            
            print(f"✅ MCQ screenshot captured with 67% zoom: {screenshot_width}x{screenshot_height}px")
            return screenshot
            
        except asyncio.TimeoutError:
            print(f"⏱️ Screenshot timeout for {url}")
            return None
        
    except Exception as e:
        print(f"❌ Error capturing screenshot for {url}: {str(e)}")
        return None

async def scrape_testbook_page_with_screenshot_ultra_robust(context: BrowserContext, url: str, topic: str, exam_type: str = "SSC") -> Optional[dict]:
    """
    Optimized screenshot scraping with faster processing and smart element detection
    ENHANCED: Added dual relevancy check for both topic AND exam type filtering
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
        
        # STEP 1: Quick topic relevance check - combined with question extraction
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
        
        # STEP 2: Check topic relevance first
        if not is_mcq_relevant(question_text, topic):
            print(f"❌ MCQ not relevant for topic '{topic}' on {url}")
            return None
        
        print(f"✅ MCQ relevant for topic '{topic}'")
        
        # STEP 3: Extract exam source information for exam type filtering
        exam_source_heading = ""
        exam_source_title = ""
        
        try:
            # Extract exam source heading
            exam_heading_element = await asyncio.wait_for(page.query_selector('div.pyp-heading'), timeout=1.0)
            if exam_heading_element:
                exam_source_heading = await asyncio.wait_for(exam_heading_element.inner_text(), timeout=1.0)
                exam_source_heading = clean_unwanted_text(exam_source_heading)
            
            # Extract exam source title
            exam_title_element = await asyncio.wait_for(page.query_selector('div.pyp-title.line-ellipsis'), timeout=1.0)
            if exam_title_element:
                exam_source_title = await asyncio.wait_for(exam_title_element.inner_text(), timeout=1.0)
                exam_source_title = clean_unwanted_text(exam_source_title)
        except asyncio.TimeoutError:
            print(f"⏱️ Timeout extracting exam source from {url}")
        
        # STEP 4: Check exam type relevance for screenshot-based extraction
        if not is_exam_type_relevant(exam_source_heading, exam_source_title, exam_type):
            print(f"❌ MCQ not relevant for exam type '{exam_type}' on {url}")
            print(f"   Exam source: '{exam_source_heading}' | '{exam_source_title}'")
            return None
        
        print(f"✅ MCQ relevant for exam type '{exam_type}' - found in exam source")
        
        # STEP 5: Both topic and exam type relevance confirmed - capture screenshot
        screenshot = await capture_page_screenshot_ultra_robust(page, url, topic)
        
        if not screenshot:
            print(f"❌ Failed to capture screenshot for {url}")
            return None
        
        return {
            "url": url,
            "screenshot": screenshot,
            "is_relevant": True,
            "exam_source_heading": exam_source_heading,
            "exam_source_title": exam_source_title
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
    """Enhanced relevance checking using comprehensive keyword dictionary"""
    from competitive_exam_keywords import enhanced_is_mcq_relevant
    return enhanced_is_mcq_relevant(question_text, search_topic)

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
            print(f"🔑 Using key: {current_key[:20]}... (Remaining: {api_key_manager.get_remaining_keys()})")
            
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

async def scrape_mcq_content_with_page_ultra_robust(page, url: str, search_topic: str, exam_type: str = "SSC") -> Optional[MCQData]:
    """
    Ultra-robust MCQ content scraping with dual relevancy check for both topic AND exam type
    ENHANCED: Added exam type filtering for text-based extraction
    """
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
        
        # STEP 1: Extract question with timeout
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
        
        # STEP 2: Check topic relevance first
        if not is_mcq_relevant(question, search_topic):
            print(f"❌ MCQ not relevant for topic '{search_topic}'")
            return None
        
        print(f"✅ MCQ relevant - topic '{search_topic}' found in question body")
        
        # STEP 3: Extract exam source information
        exam_source_heading = ""
        exam_source_title = ""
        
        try:
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
                print(f"⏱️ Timeout extracting exam source from {url}")
        except Exception as e:
            print(f"⚠️ Error extracting exam source: {e}")
        
        # STEP 4: Check exam type relevance for text-based extraction
        if not is_exam_type_relevant(exam_source_heading, exam_source_title, exam_type):
            print(f"❌ MCQ not relevant for exam type '{exam_type}' on {url}")
            print(f"   Exam source: '{exam_source_heading}' | '{exam_source_title}'")
            return None
        
        print(f"✅ MCQ relevant for exam type '{exam_type}' - found in exam source")
        
        # STEP 5: Both topic and exam type relevance confirmed - extract remaining elements
        options = []
        answer = ""
        
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

async def scrape_mcq_content_ultra_robust(url: str, search_topic: str, exam_type: str = "SSC") -> Optional[MCQData]:
    """
    Ultra-robust MCQ scraping with maximum error handling
    ENHANCED: Added exam type parameter for filtering
    """
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
            
            # Scrape content with exam type filtering
            result = await scrape_mcq_content_with_page_ultra_robust(page, url, search_topic, exam_type)
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
        success_color = HexColor('#48bb78')      # Green
        warning_color = HexColor('#ed8936')      # Orange
        text_color = HexColor('#2d3748')         # Dark Gray
        light_color = HexColor('#f7fafc')        # Very Light Blue
        gradient_start = HexColor('#667eea')     # Purple-Blue
        gradient_end = HexColor('#764ba2')       # Purple
        gold_color = HexColor('#d69e2e')         # Gold for accents
        
        # 📚 ENHANCED Typography Styles with Beautiful Design Elements
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=36,
            spaceAfter=35,
            alignment=TA_CENTER,
            textColor=primary_color,
            fontName='Helvetica-Bold',
            borderWidth=3,
            borderColor=accent_color,
            borderPadding=20,
            backColor=light_color,
            borderRadius=10,
            leading=45
        )
        
        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Normal'],
            fontSize=18,
            spaceAfter=30,
            alignment=TA_CENTER,
            textColor=secondary_color,
            fontName='Helvetica-Bold',
            borderWidth=2,
            borderColor=gold_color,
            borderPadding=12,
            backColor=white,
            borderRadius=5
        )
        
        question_header_style = ParagraphStyle(
            'QuestionHeaderStyle',
            parent=styles['Normal'],
            fontSize=18,
            spaceAfter=20,
            fontName='Helvetica-Bold',
            textColor=white,
            borderWidth=3,
            borderColor=primary_color,
            borderPadding=15,
            backColor=gradient_start,
            alignment=TA_CENTER,
            borderRadius=12
        )
        
        question_style = ParagraphStyle(
            'QuestionStyle',
            parent=styles['Normal'],
            fontSize=14,
            spaceAfter=18,
            textColor=text_color,
            fontName='Helvetica',
            borderWidth=2,
            borderColor=accent_color,
            borderPadding=15,
            backColor=light_color,
            leftIndent=20,
            rightIndent=20,
            borderRadius=8
        )
        
        option_style = ParagraphStyle(
            'OptionStyle',
            parent=styles['Normal'],
            fontSize=13,
            spaceAfter=12,
            textColor=text_color,
            fontName='Helvetica',
            leftIndent=30,
            rightIndent=20,
            borderWidth=1,
            borderColor=secondary_color,
            borderPadding=10,
            backColor=white,
            borderRadius=5
        )
        
        answer_style = ParagraphStyle(
            'AnswerStyle',
            parent=styles['Normal'],
            fontSize=13,
            spaceAfter=15,
            textColor=primary_color,
            fontName='Helvetica',
            borderWidth=2,
            borderColor=success_color,
            borderPadding=15,
            backColor=light_color,
            leftIndent=20,
            rightIndent=20,
            borderRadius=8
        )
        
        # 🎯 Beautiful Story Elements
        story = []
        
        # 📋 STUNNING COVER PAGE with Graphics
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(Paragraph("🎓 PREMIUM MCQ COLLECTION", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph(f"📚 Subject: {topic.upper()}", subtitle_style))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(DecorativeSeparator(doc.width, 0.15*inch))
        story.append(Spacer(1, 0.4*inch))
        
        # PREMIUM ENHANCED STATISTICS TABLE DESIGN
        stats_data = [
            ['📊 COLLECTION ANALYTICS', ''],
            ['🎯 Search Topic', f'{topic}'],
            ['✅ Total Quality Questions', f'{len(mcqs)}'],
            ['🔍 Smart Filtering Applied', 'Ultra-Premium Topic-based'],
            ['📅 Generated On', f'{datetime.now().strftime("%B %d, %Y at %I:%M %p")}'],
            ['🌐 Authoritative Source', 'Testbook.com (Premium Grade)'],
            ['🏆 Quality Assurance', 'Professional Excellence'],
            ['⚡ Processing Method', 'Ultra-Robust AI Enhanced']
        ]
        
        stats_table = Table(stats_data, colWidths=[3*inch, 2.5*inch])
        
        # Enhanced premium table styling with alternating rows and beautiful borders
        stats_table_style = [
            # Header styling with gradient-like effect
            ('BACKGROUND', (0, 0), (-1, 0), primary_color),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),  # Changed from CENTER to LEFT for better readability
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 14),  # Increased font size
            ('LEFTPADDING', (0, 0), (-1, -1), 15),  # Enhanced padding
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, 0), 15),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 15),
            
            # Enhanced data rows with alternating backgrounds
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 12),  # Increased font size for better readability
            ('TOPPADDING', (0, 1), (-1, -1), 12),  # Enhanced padding
            ('BOTTOMPADDING', (0, 1), (-1, -1), 12),
            
            # Alternating row colors for better visual separation
            ('BACKGROUND', (0, 1), (-1, 1), light_color),
            ('BACKGROUND', (0, 2), (-1, 2), white),
            ('BACKGROUND', (0, 3), (-1, 3), light_color),
            ('BACKGROUND', (0, 4), (-1, 4), white),
            ('BACKGROUND', (0, 5), (-1, 5), light_color),
            ('BACKGROUND', (0, 6), (-1, 6), white),
            ('BACKGROUND', (0, 7), (-1, 7), light_color),
            ('BACKGROUND', (0, 8), (-1, 8), white),
            
            # Beautiful border styling with accent colors
            ('GRID', (0, 0), (-1, -1), 2, accent_color),  # Enhanced border width
            ('LINEBELOW', (0, 0), (-1, 0), 3, secondary_color),  # Thicker header underline
            ('LINEBEFORE', (0, 0), (0, -1), 3, accent_color),  # Left accent border
            ('LINEAFTER', (-1, 0), (-1, -1), 3, accent_color),  # Right accent border
            
            # Enhanced vertical alignment and spacing
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Subtle text colors for better readability
            ('TEXTCOLOR', (0, 1), (-1, -1), text_color),
            ('TEXTCOLOR', (1, 1), (-1, -1), primary_color),  # Values in primary color for emphasis
        ]
        
        stats_table.setStyle(TableStyle(stats_table_style))
        
        story.append(stats_table)
        story.append(Spacer(1, 0.4*inch))
        
        # Replace simple separator with premium DecorativeSeparator
        story.append(DecorativeSeparator(doc.width, 0.15*inch))
        story.append(PageBreak())
        
        # 📝 ENHANCED MCQ CONTENT with Beautiful Styling
        for i, mcq in enumerate(mcqs, 1):
            # Professional Question header with graphics
            story.append(Paragraph(f"🎯 QUESTION {i} OF {len(mcqs)} 🎯", question_header_style))
            story.append(Spacer(1, 0.15*inch))
            
            # Exam source with beautiful styling - FIXED: Use correct MCQData attributes
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
            if mcq.question:
                question_text = mcq.question.replace('\n', '<br/>')
                story.append(Paragraph(f"❓ <b>QUESTION:</b><br/><br/>{question_text}", question_style))
            
            story.append(Spacer(1, 0.15*inch))
            
            # Beautiful Options with enhanced styling
            if mcq.options:
                for j, option in enumerate(mcq.options, 1):
                    option_letter = chr(64 + j)  # A, B, C, D...
                    option_text = option.replace('\n', '<br/>')
                    story.append(Paragraph(f"<b>{option_letter})</b> {option_text}", option_style))
            
            story.append(Spacer(1, 0.15*inch))
            
            # Answer with premium styling
            if mcq.answer:
                answer_text = mcq.answer.replace('\n', '<br/>')
                story.append(Paragraph(f"💡 <b>CORRECT ANSWER & EXPLANATION:</b><br/><br/>{answer_text}", answer_style))
            
            # Add decorative separator between questions (except for the last one)
            if i < len(mcqs):
                story.append(Spacer(1, 0.3*inch))
                story.append(DecorativeSeparator(doc.width, 0.1*inch))
                story.append(PageBreak())
        
        # PROFESSIONAL FOOTER
        story.append(Spacer(1, 0.5*inch))
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph("⭐ <b>PREMIUM COLLECTION COMPLETE</b> ⭐", 
            ParagraphStyle('FooterTitle', parent=styles['Normal'], 
                fontSize=16, alignment=TA_CENTER, textColor=gold_color,
                fontName='Helvetica-Bold', spaceAfter=15)))
        
        story.append(Paragraph(f"Generated with ❤️ by Ultra-Robust MCQ Scraper v3.0", 
            ParagraphStyle('CreditSubtext', parent=styles['Normal'], 
                fontSize=14, alignment=TA_CENTER, textColor=accent_color,
                fontName='Helvetica-Oblique')))
        
        story.append(Spacer(1, 0.5*inch))
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        
        # Build the beautiful PDF
        doc.build(story)
        
        print(f"✅ BEAUTIFUL PROFESSIONAL PDF generated successfully: {filename} with {len(mcqs)} MCQs")
        return filename
        
    except Exception as e:
        print(f"❌ Error generating beautiful PDF: {e}")
        raise

def generate_image_based_pdf(screenshots_data: List[dict], topic: str, exam_type: str = "SSC") -> str:
    """Generate BEAUTIFUL PROFESSIONAL image-based PDF with enhanced design, graphics, and premium styling"""
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.lib.colors import HexColor, white
        import io
        from PIL import Image as PILImage
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"mcq_screenshots_{topic}_{exam_type}_{timestamp}.pdf"
        
        pdf_dir = get_pdf_directory()
        filepath = pdf_dir / filename
        
        doc = SimpleDocTemplate(str(filepath), pagesize=A4,
                              topMargin=0.6*inch, bottomMargin=0.6*inch,
                              leftMargin=0.6*inch, rightMargin=0.6*inch)
        story = []
        
        styles = getSampleStyleSheet()
        
        # 🎨 PREMIUM Color Palette - Professional & Eye-catching
        primary_color = HexColor('#1a365d')      # Deep Navy Blue
        secondary_color = HexColor('#2b6cb0')    # Medium Blue  
        accent_color = HexColor('#38b2ac')       # Teal
        success_color = HexColor('#48bb78')      # Green
        warning_color = HexColor('#ed8936')      # Orange
        text_color = HexColor('#2d3748')         # Dark Gray
        light_color = HexColor('#f7fafc')        # Very Light Blue
        gradient_start = HexColor('#667eea')     # Purple-Blue
        gradient_end = HexColor('#764ba2')       # Purple
        gold_color = HexColor('#d69e2e')         # Gold for accents
        
        # 📚 ENHANCED Typography Styles with Beautiful Design Elements
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=36,
            spaceAfter=35,
            alignment=TA_CENTER,
            textColor=primary_color,
            fontName='Helvetica-Bold',
            borderWidth=3,
            borderColor=accent_color,
            borderPadding=20,
            backColor=light_color,
            borderRadius=10,
            leading=45
        )
        
        subtitle_style = ParagraphStyle(
            'CustomSubtitle',
            parent=styles['Normal'],
            fontSize=18,
            spaceAfter=30,
            alignment=TA_CENTER,
            textColor=secondary_color,
            fontName='Helvetica-Bold',
            borderWidth=2,
            borderColor=gold_color,
            borderPadding=12,
            backColor=white,
            borderRadius=5
        )
        
        question_header_style = ParagraphStyle(
            'QuestionHeaderStyle',
            parent=styles['Normal'],
            fontSize=18,
            spaceAfter=20,
            fontName='Helvetica-Bold',
            textColor=white,
            borderWidth=3,
            borderColor=primary_color,
            borderPadding=15,
            backColor=gradient_start,
            alignment=TA_CENTER,
            borderRadius=12
        )
        
        # 📋 STUNNING COVER PAGE with Graphics
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(Paragraph("🎓 PREMIUM MCQ COLLECTION", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph(f"📚 Subject: {topic.upper()}", subtitle_style))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(DecorativeSeparator(doc.width, 0.15*inch))
        story.append(Spacer(1, 0.4*inch))
        
        # PREMIUM ENHANCED STATISTICS TABLE DESIGN
        stats_data = [
            ['📊 COLLECTION ANALYTICS', ''],
            ['🎯 Search Topic', f'{topic}'],
            ['✅ Total Quality Questions', f'{len(screenshots_data)}'],
            ['🔍 Smart Filtering Applied', 'Ultra-Premium Topic-based'],
            ['📅 Generated On', f'{datetime.now().strftime("%B %d, %Y at %I:%M %p")}'],
            ['🌐 Authoritative Source', 'Testbook.com (Premium Grade)'],
            ['🏆 Quality Assurance', 'Professional Excellence'],
            ['⚡ Processing Method', 'Ultra-Robust AI Enhanced']
        ]
        
        stats_table = Table(stats_data, colWidths=[3*inch, 2.5*inch])
        
        # Enhanced premium table styling with alternating rows and beautiful borders
        stats_table_style = [
            # Header styling with gradient-like effect
            ('BACKGROUND', (0, 0), (-1, 0), primary_color),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),  # Changed from CENTER to LEFT for better readability
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 14),  # Increased font size
            ('LEFTPADDING', (0, 0), (-1, -1), 15),  # Enhanced padding
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ('TOPPADDING', (0, 0), (-1, 0), 15),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 15),
            
            # Enhanced data rows with alternating backgrounds
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 12),  # Increased font size for better readability
            ('TOPPADDING', (0, 1), (-1, -1), 12),  # Enhanced padding
            ('BOTTOMPADDING', (0, 1), (-1, -1), 12),
            
            # Alternating row colors for better visual separation
            ('BACKGROUND', (0, 1), (-1, 1), light_color),
            ('BACKGROUND', (0, 2), (-1, 2), white),
            ('BACKGROUND', (0, 3), (-1, 3), light_color),
            ('BACKGROUND', (0, 4), (-1, 4), white),
            ('BACKGROUND', (0, 5), (-1, 5), light_color),
            ('BACKGROUND', (0, 6), (-1, 6), white),
            ('BACKGROUND', (0, 7), (-1, 7), light_color),
            ('BACKGROUND', (0, 8), (-1, 8), white),
            
            # Beautiful border styling with accent colors
            ('GRID', (0, 0), (-1, -1), 2, accent_color),  # Enhanced border width
            ('LINEBELOW', (0, 0), (-1, 0), 3, secondary_color),  # Thicker header underline
            ('LINEBEFORE', (0, 0), (0, -1), 3, accent_color),  # Left accent border
            ('LINEAFTER', (-1, 0), (-1, -1), 3, accent_color),  # Right accent border
            
            # Enhanced vertical alignment and spacing
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            
            # Subtle text colors for better readability
            ('TEXTCOLOR', (0, 1), (-1, -1), text_color),
            ('TEXTCOLOR', (1, 1), (-1, -1), primary_color),  # Values in primary color for emphasis
        ]
        
        stats_table.setStyle(TableStyle(stats_table_style))
        
        story.append(stats_table)
        story.append(Spacer(1, 0.4*inch))
        
        # Replace simple separator with premium DecorativeSeparator
        story.append(DecorativeSeparator(doc.width, 0.15*inch))
        story.append(PageBreak())
        
        # 📝 ENHANCED MCQ CONTENT with Beautiful Styling
        for i, screenshot_item in enumerate(screenshots_data, 1):
            # Professional Question header with graphics
            story.append(Paragraph(f"🎯 QUESTION {i} OF {len(screenshots_data)} 🎯", question_header_style))
            story.append(Spacer(1, 0.15*inch))
            
            # Source URL with beautiful styling
            url_style = ParagraphStyle(
                'URL',
                parent=styles['Normal'],
                fontSize=10,
                textColor=accent_color,
                alignment=TA_CENTER,
                fontName='Helvetica-Oblique',
                borderWidth=1,
                borderColor=accent_color,
                borderPadding=8,
                backColor=light_color,
                borderRadius=5
            )
            story.append(Paragraph(f"🌐 Source: {screenshot_item['url']}", url_style))
            story.append(Spacer(1, 0.2*inch))
            
            # Convert and process screenshot
            screenshot_pil = PILImage.open(io.BytesIO(screenshot_item['screenshot']))
            
            img_buffer = io.BytesIO()
            screenshot_pil.save(img_buffer, format='PNG')
            img_buffer.seek(0)
            
            # Calculate enhanced dimensions for better display
            page_width = A4[0] - 1.2*inch
            page_height = A4[1] - 3*inch
            
            img_width, img_height = screenshot_pil.size
            aspect_ratio = img_width / img_height
            
            if aspect_ratio > 1:  # Landscape
                display_width = min(page_width, 7*inch)
                display_height = display_width / aspect_ratio
            else:  # Portrait
                display_height = min(page_height, 9*inch)
                display_width = display_height * aspect_ratio
            
            # Add enhanced image with border styling
            img = Image(img_buffer, width=display_width, height=display_height)
            story.append(img)
            story.append(Spacer(1, 0.3*inch))
            
            # Beautiful separator between questions
            story.append(DecorativeSeparator(doc.width, 0.1*inch))
            story.append(Spacer(1, 0.2*inch))
            
            if i < len(screenshots_data):
                story.append(PageBreak())
        
        # 🌟 BEAUTIFUL CREDIT PAGE - "Made By HEMANT SINGH"
        story.append(PageBreak())
        story.append(Spacer(1, 2*inch))
        
        # Credit page with stunning design
        credit_style = ParagraphStyle(
            'CreditStyle',
            parent=styles['Normal'],
            fontSize=28,
            alignment=TA_CENTER,
            textColor=primary_color,
            fontName='Helvetica-Bold',
            borderWidth=4,
            borderColor=gold_color,
            borderPadding=25,
            backColor=light_color,
            borderRadius=15,
            spaceAfter=20
        )
        
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        story.append(Spacer(1, 0.5*inch))
        
        story.append(Paragraph("✨ CREATED BY ✨", 
            ParagraphStyle('CreditHeader', parent=styles['Normal'], 
                fontSize=18, alignment=TA_CENTER, textColor=secondary_color,
                fontName='Helvetica-Bold', spaceAfter=20)))
        
        story.append(Paragraph("🎯 HEMANT SINGH 🎯", credit_style))
        
        story.append(Spacer(1, 0.3*inch))
        story.append(Paragraph("Premium MCQ Collection Designer", 
            ParagraphStyle('CreditSubtext', parent=styles['Normal'], 
                fontSize=14, alignment=TA_CENTER, textColor=accent_color,
                fontName='Helvetica-Oblique')))
        
        story.append(Spacer(1, 0.5*inch))
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        
        # Build the beautiful PDF
        doc.build(story)
        
        print(f"✅ BEAUTIFUL PROFESSIONAL image PDF generated: {filename} with {len(screenshots_data)} screenshots")
        return filename
        
    except Exception as e:
        print(f"❌ Error generating beautiful image PDF: {e}")
        raise

async def process_mcq_extraction(job_id: str, topic: str, exam_type: str = "SSC", pdf_format: str = "text"):
    """Ultra-robust MCQ extraction with persistent job tracking"""
    try:
        update_job_progress(job_id, "running", f"🔍 Searching for {exam_type} '{topic}' results with ultra-smart filtering...")
        
        # Search for links
        links = await search_google_custom(topic, exam_type)
        
        if not links:
            update_job_progress(job_id, "completed", f"❌ No {exam_type} results found for '{topic}'. Please try another topic.", 
                              total_links=0, processed_links=0, mcqs_found=0)
            return
        
        update_job_progress(job_id, "running", f"✅ Found {len(links)} {exam_type} links. Starting ultra-smart filtering extraction...", 
                          total_links=len(links))
        
        if pdf_format == "image":
            await process_screenshot_extraction_ultra_robust(job_id, topic, exam_type, links)
        else:
            await process_text_extraction_ultra_robust(job_id, topic, exam_type, links)
        
    except Exception as e:
        error_message = str(e)
        print(f"❌ Critical error in process_mcq_extraction: {e}")
        update_job_progress(job_id, "error", f"❌ Error: {error_message}")

async def process_text_extraction_ultra_robust(job_id: str, topic: str, exam_type: str, links: List[str]):
    """
    Ultra-robust text extraction with persistent job tracking
    ENHANCED: Added exam type parameter for dual relevancy filtering
    """
    try:
        await browser_pool.initialize()
        
        mcqs = []
        relevant_mcqs = 0
        irrelevant_mcqs = 0
        
        print(f"🚀 Starting ULTRA-ROBUST text processing: {len(links)} links")
        
        for i, url in enumerate(links):
            print(f"🔍 Processing link {i + 1}/{len(links)}: {url}")
            
            # Update progress with persistent storage
            current_progress = f"🔍 Processing link {i + 1}/{len(links)} - Ultra-smart filtering enabled..."
            update_job_progress(job_id, "running", current_progress, 
                              processed_links=i, mcqs_found=len(mcqs))
            
            try:
                # ENHANCED: Pass exam_type for dual relevancy filtering (topic + exam type)
                result = await scrape_mcq_content_ultra_robust(url, topic, exam_type)
                
                if result:
                    mcqs.append(result)
                    relevant_mcqs += 1
                    print(f"✅ Found relevant MCQ {i + 1}/{len(links)} - Total: {len(mcqs)}")
                else:
                    irrelevant_mcqs += 1
                    print(f"⚠️ Skipped irrelevant MCQ {i + 1}/{len(links)}")
                    
            except Exception as e:
                print(f"❌ Error processing link {i + 1}: {e}")
                irrelevant_mcqs += 1
            
            # Update progress
            update_job_progress(job_id, "running", 
                              f"✅ Processed {i + 1}/{len(links)} links - Found {len(mcqs)} relevant MCQs", 
                              processed_links=i + 1, mcqs_found=len(mcqs))
            
            # Small delay
            if i < len(links) - 1:
                await asyncio.sleep(1)
        
        await browser_pool.close()
        
        if not mcqs:
            update_job_progress(job_id, "completed", 
                              f"❌ No relevant MCQs found for '{topic}' and exam type '{exam_type}' across {len(links)} links.", 
                              total_links=len(links), processed_links=len(links), mcqs_found=0)
            return
        
        # Generate PDF
        final_message = f"✅ Ultra-smart filtering complete! Found {relevant_mcqs} relevant MCQs from {len(links)} total links."
        update_job_progress(job_id, "running", final_message + " Generating PDF...", 
                          total_links=len(links), processed_links=len(links), mcqs_found=len(mcqs))
        
        try:
            filename = generate_pdf(mcqs, topic, job_id, relevant_mcqs, irrelevant_mcqs, len(links))
            pdf_url = f"/api/download-pdf/{filename}"
            
            generated_pdfs[job_id] = {
                "filename": filename,
                "topic": topic,
                "exam_type": exam_type,
                "mcqs_count": len(mcqs),
                "generated_at": datetime.now()
            }
            
            success_message = f"🎉 SUCCESS! Generated PDF with {len(mcqs)} relevant MCQs for topic '{topic}' and exam type '{exam_type}'."
            update_job_progress(job_id, "completed", success_message, 
                              total_links=len(links), processed_links=len(links), 
                              mcqs_found=len(mcqs), pdf_url=pdf_url)
            
            print(f"✅ Job {job_id} completed successfully with {len(mcqs)} MCQs")
            
        except Exception as e:
            print(f"❌ Error generating PDF: {e}")
            update_job_progress(job_id, "error", f"❌ Error generating PDF: {str(e)}")
    
    except Exception as e:
        print(f"❌ Critical error in text extraction: {e}")
        update_job_progress(job_id, "error", f"❌ Critical error: {str(e)}")
        await browser_pool.close()

async def process_screenshot_extraction_ultra_robust(job_id: str, topic: str, exam_type: str, links: List[str]):
    """
    Optimized screenshot extraction with faster processing and smart context reuse
    ENHANCED: Added exam type filtering for screenshot-based extraction
    """
    context = None
    try:
        await browser_pool.initialize()
        
        screenshot_data = []
        relevant_mcqs = 0
        irrelevant_mcqs = 0
        
        print(f"🚀 Starting optimized screenshot processing: {len(links)} links")
        
        # Smart batch processing - reuse context for multiple pages
        batch_size = 5
        current_context_reuses = 0
        max_context_reuses = 8  # Reuse context up to 8 times before renewal
        
        for i, url in enumerate(links):
            print(f"📸 Processing screenshot {i + 1}/{len(links)}: {url}")
            
            # Update progress with persistent storage
            current_progress = f"📸 Capturing screenshot {i + 1}/{len(links)} - Smart processing enabled..."
            update_job_progress(job_id, "running", current_progress, 
                              processed_links=i, mcqs_found=len(screenshot_data))
            
            # Smart context management - reuse when possible
            if context is None or current_context_reuses >= max_context_reuses:
                if context:
                    try:
                        await context.close()
                    except:
                        pass
                
                try:
                    context = await browser_pool.get_context()
                    current_context_reuses = 0
                    print(f"✅ New browser context created (reuse count reset)")
                except Exception as e:
                    print(f"❌ Failed to create browser context: {e}")
                    irrelevant_mcqs += 1
                    continue
            
            # Single attempt processing - trust our optimized functions
            try:
                # ENHANCED: Pass exam_type for dual relevancy filtering (topic + exam type)
                result = await scrape_testbook_page_with_screenshot_ultra_robust(context, url, topic, exam_type)
                
                if result and result.get('is_relevant'):
                    screenshot_data.append(result)
                    relevant_mcqs += 1
                    print(f"✅ Captured relevant screenshot {i + 1}/{len(links)} - Total: {len(screenshot_data)}")
                else:
                    irrelevant_mcqs += 1
                    print(f"⚠️ Skipped irrelevant screenshot {i + 1}/{len(links)}")
                
                current_context_reuses += 1
                
            except Exception as e:
                print(f"❌ Error capturing screenshot {i + 1}: {e}")
                irrelevant_mcqs += 1
                # Reset context on error
                if context:
                    try:
                        await context.close()
                    except:
                        pass
                    context = None
            
            # Update progress
            update_job_progress(job_id, "running", 
                              f"✅ Processed {i + 1}/{len(links)} links - Captured {len(screenshot_data)} relevant screenshots", 
                              processed_links=i + 1, mcqs_found=len(screenshot_data))
            
            # Minimal delay for stability - reduced from 1 second to 0.3 seconds
            if i < len(links) - 1:
                await asyncio.sleep(0.3)
        
        # Clean up context
        if context:
            try:
                await context.close()
            except:
                pass
        
        await browser_pool.close()
        
        if not screenshot_data:
            update_job_progress(job_id, "completed", 
                              f"❌ No relevant screenshots captured for '{topic}' and exam type '{exam_type}'.", 
                              total_links=len(links), processed_links=len(links), mcqs_found=0)
            return
        
        # Generate PDF
        try:
            final_message = f"✅ Screenshot capture complete! Captured {relevant_mcqs} relevant screenshots from {len(links)} total links."
            update_job_progress(job_id, "running", final_message + " Generating PDF...", 
                              total_links=len(links), processed_links=len(links), mcqs_found=len(screenshot_data))
            
            filename = generate_image_based_pdf(screenshot_data, topic, exam_type)
            pdf_url = f"/api/download-pdf/{filename}"
            
            generated_pdfs[job_id] = {
                "filename": filename,
                "topic": topic,
                "exam_type": exam_type,
                "mcqs_count": len(screenshot_data),
                "generated_at": datetime.now()
            }
            
            success_message = f"🎉 SUCCESS! Generated optimized image-based PDF with {len(screenshot_data)} relevant screenshots for topic '{topic}' and exam type '{exam_type}'."
            update_job_progress(job_id, "completed", success_message, 
                              total_links=len(links), processed_links=len(links), 
                              mcqs_found=len(screenshot_data), pdf_url=pdf_url)
            
            print(f"✅ Optimized screenshot job {job_id} completed successfully with {len(screenshot_data)} images")
            
        except Exception as e:
            print(f"❌ Error generating image PDF: {e}")
            update_job_progress(job_id, "error", f"❌ Error generating image PDF: {str(e)}")
    
    except Exception as e:
        print(f"❌ Critical error in screenshot extraction: {e}")
        update_job_progress(job_id, "error", f"❌ Critical error: {str(e)}")
        await browser_pool.close()

@app.get("/api/health")
async def health_check():
    """Health check endpoint with browser status"""
    return {
        "status": "healthy",
        "version": "3.0.0",
        "browser_status": {
            "installed": browser_installation_state["is_installed"],
            "installation_attempted": browser_installation_state["installation_attempted"],
            "installation_error": browser_installation_state.get("installation_error"),
            "browser_pool_initialized": browser_pool.is_initialized
        },
        "active_jobs": len(persistent_storage.jobs),
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/generate-mcq-pdf")
async def generate_mcq_pdf(request: SearchRequest, background_tasks: BackgroundTasks):
    """Generate MCQ PDF with ultra-robust error handling"""
    job_id = str(uuid.uuid4())
    
    # Validate inputs
    if not request.topic.strip():
        raise HTTPException(status_code=400, detail="Topic is required")
    
    if request.exam_type not in ["SSC", "BPSC"]:
        raise HTTPException(status_code=400, detail="Exam type must be SSC or BPSC")
    
    if request.pdf_format not in ["text", "image"]:
        raise HTTPException(status_code=400, detail="PDF format must be 'text' or 'image'")
    
    # Initialize job progress with persistent storage
    update_job_progress(
        job_id, 
        "running", 
        f"🚀 Starting ultra-robust {request.exam_type} MCQ extraction for '{request.topic}' ({request.pdf_format} format)..."
    )
    
    # Start background task
    background_tasks.add_task(
        process_mcq_extraction,
        job_id=job_id,
        topic=request.topic.strip(),
        exam_type=request.exam_type,
        pdf_format=request.pdf_format
    )
    
    return {"job_id": job_id, "status": "started"}

@app.get("/api/job-status/{job_id}")
async def get_job_status(job_id: str):
    """Get job status with persistent storage"""
    job_data = persistent_storage.get_job(job_id)
    
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return JobStatus(**job_data)

@app.get("/api/download-pdf/{filename}")
async def download_pdf(filename: str):
    """Download generated PDF with enhanced error handling"""
    try:
        pdf_dir = get_pdf_directory()
        file_path = pdf_dir / filename
        
        if not file_path.exists():
            print(f"❌ PDF file not found: {file_path}")
            raise HTTPException(status_code=404, detail="PDF file not found")
        
        print(f"✅ Serving PDF file: {file_path}")
        return FileResponse(
            path=str(file_path),
            filename=filename,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error serving PDF file: {e}")
        raise HTTPException(status_code=500, detail="Error serving PDF file")

@app.get("/api/cleanup-old-jobs")
async def cleanup_old_jobs():
    """Manual cleanup of old jobs"""
    try:
        persistent_storage.cleanup_old_jobs()
        return {"message": "Old jobs cleaned up successfully", "remaining_jobs": len(persistent_storage.jobs)}
    except Exception as e:
        print(f"❌ Error cleaning up old jobs: {e}")
        raise HTTPException(status_code=500, detail="Error cleaning up old jobs")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
