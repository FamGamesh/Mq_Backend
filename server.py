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
                print(f"ЁЯУВ Loaded {len(self.jobs)} jobs from persistent storage")
            else:
                self.jobs = {}
                print("ЁЯУВ No persistent storage found, starting fresh")
        except Exception as e:
            print(f"тЪая╕П Error loading jobs from storage: {e}")
            self.jobs = {}
    
    def save_jobs(self):
        """Save jobs to persistent storage"""
        try:
            with open(self.storage_file, 'wb') as f:
                pickle.dump(self.jobs, f)
        except Exception as e:
            print(f"тЪая╕П Error saving jobs to storage: {e}")
    
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
            
            print(f"ЁЯУК Job {job_id}: {status} - {progress}")
            
        except Exception as e:
            print(f"тЪая╕П Error updating job progress: {e}")
    
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
                print(f"ЁЯз╣ Cleaned up {len(jobs_to_remove)} old jobs")
                
        except Exception as e:
            print(f"тЪая╕П Error cleaning up old jobs: {e}")

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
                
                print("тЪая╕П Browser connection lost, reinitializing...")
                await self._cleanup()
                self.is_initialized = False
            
            if self.is_initialized:
                return
            
            print("ЁЯЪА Initializing Ultra-Robust Browser Pool Manager...")
            
            # Check browser installation
            if not browser_installation_state["is_installed"]:
                print("тЪая╕П Browsers not installed, attempting installation...")
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
                    
                    print(f"тЬЕ Ultra-Robust Browser Pool Manager initialized successfully! (Restart #{self.restart_count})")
                    return
                    
                except Exception as e:
                    print(f"тЭМ Browser initialization attempt {attempt + 1} failed: {e}")
                    self.last_error = str(e)
                    await self._cleanup()
                    
                    if attempt < max_init_attempts - 1:
                        wait_time = (2 ** attempt) * 2
                        print(f"тП│ Waiting {wait_time}s before retry...")
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
                print("тЬЕ Browser installation successful")
            else:
                raise Exception(f"Browser installation failed: {result.stderr}")
                
        except Exception as e:
            print(f"тЭМ Browser installation error: {e}")
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
        print("ЁЯФз Handling browser failure with memory cleanup...")
        await self._cleanup()
        self.retry_count += 1
        
        # Force garbage collection
        import gc
        gc.collect()
        
        # If too many failures, wait longer
        if self.retry_count > 3:
            print(f"тЪая╕П Multiple browser failures ({self.retry_count}), waiting extra time...")
            await asyncio.sleep(10)
    
    async def _emergency_recovery(self):
        """Emergency recovery procedure"""
        print("ЁЯЪи Emergency recovery procedure initiated...")
        
        # Force cleanup everything
        await self._cleanup()
        
        # Kill any remaining browser processes
        try:
            subprocess.run(["pkill", "-f", "chromium"], capture_output=True)
            subprocess.run(["pkill", "-f", "chrome"], capture_output=True)
            print("ЁЯФД Killed remaining browser processes")
        except:
            pass
        
        # Clear temporary files
        try:
            subprocess.run(["rm", "-rf", "/tmp/playwright_*"], shell=True, capture_output=True)
            print("ЁЯз╣ Cleared temporary files")
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
            print("тП▒я╕П Cleanup timeout, forcing termination")
        except Exception as e:
            print(f"тЪая╕П Error during cleanup: {e}")
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
        print("ЁЯФД Closing Ultra-Robust Browser Pool Manager...")
        await self._cleanup()
        print("тЬЕ Ultra-Robust Browser Pool Manager closed")

# Global ultra-robust browser pool
browser_pool = UltraRobustBrowserPoolManager()

def force_install_browsers():
    """Force install browsers with cloud deployment friendly approach"""
    print("ЁЯФД Starting cloud-compatible browser installation...")
    
    try:
        # Ensure directory exists
        os.makedirs("/tmp/pw-browsers", exist_ok=True)
        
        # Set environment variables for installation
        env = os.environ.copy()
        env['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
        env['PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'] = '0'
        
        # Install system dependencies first
        print("ЁЯУж Installing system dependencies...")
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
                print(f"   тЬЕ {dep_cmd.split()[2] if len(dep_cmd.split()) > 2 else dep_cmd}")
            except:
                print(f"   тЪая╕П Failed: {dep_cmd}")
        
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
                print(f"ЁЯФД Trying: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env
                )
                
                if result.returncode == 0:
                    print(f"тЬЕ SUCCESS with: {cmd}")
                    print(f"   Output: {result.stdout[:200]}...")
                    
                    if verify_browser_installation():
                        print("тЬЕ Browser installation verified!")
                        return True
                    else:
                        print("тЪая╕П Installation completed but verification failed")
                        continue
                else:
                    print(f"тЭМ FAILED: {cmd}")
                    print(f"   Error: {result.stderr[:200]}...")
                    
            except subprocess.TimeoutExpired:
                print(f"тП▒я╕П TIMEOUT: {cmd}")
            except Exception as e:
                print(f"ЁЯТе ERROR: {cmd} - {str(e)}")
        
        print("тЭМ All installation methods failed")
        return False
        
    except Exception as e:
        print(f"ЁЯТе Critical error in browser installation: {e}")
        return False

def verify_browser_installation():
    """Verify browser installation with multiple checks"""
    try:
        browser_path = "/tmp/pw-browsers"
        
        if not os.path.exists(browser_path):
            print("тЭМ Browser directory doesn't exist")
            return False
        
        # Check for browser directories
        browser_found = False
        executable_found = False
        
        for item in os.listdir(browser_path):
            item_path = os.path.join(browser_path, item)
            if os.path.isdir(item_path) and ("chromium" in item.lower() or "chrome" in item.lower()):
                browser_found = True
                print(f"тЬЕ Found browser directory: {item}")
                
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
                        print(f"тЬЕ Found executable: {executable}")
                        if os.access(executable, os.X_OK):
                            print(f"тЬЕ Executable is runnable: {executable}")
                            return True
                        else:
                            print(f"тЪая╕П Executable not runnable: {executable}")
        
        if browser_found and not executable_found:
            print("тЪая╕П Browser directory found but no executables")
        elif not browser_found:
            print("тЭМ No browser directories found")
        
        return False
        
    except Exception as e:
        print(f"тЭМ Error verifying browser installation: {e}")
        return False

def install_browsers_blocking():
    """Install browsers in blocking mode during startup"""
    global browser_installation_state
    
    print("ЁЯЪА Starting browser installation check...")
    
    if verify_browser_installation():
        browser_installation_state["is_installed"] = True
        print("тЬЕ Browsers already installed and verified!")
        return True
    
    browser_installation_state["installation_in_progress"] = True
    browser_installation_state["installation_attempted"] = True
    
    print("ЁЯФД Browsers not found. Starting installation...")
    
    try:
        success = install_with_python_module()
        
        if success:
            browser_installation_state["installation_in_progress"] = False
            browser_installation_state["is_installed"] = True
            print("ЁЯОЙ Browser installation completed successfully!")
            return True
        
        success = force_install_browsers()
        
        if success:
            browser_installation_state["installation_in_progress"] = False
            browser_installation_state["is_installed"] = True
            print("ЁЯОЙ Browser installation completed successfully!")
            return True
        
        success = install_with_script()
        
        if success:
            browser_installation_state["installation_in_progress"] = False
            browser_installation_state["is_installed"] = True
            print("ЁЯОЙ Browser installation completed successfully!")
            return True
        
    except Exception as e:
        print(f"ЁЯТе Error during installation strategies: {e}")
    
    error_msg = "Failed to install Playwright browsers after trying all strategies"
    browser_installation_state["installation_in_progress"] = False
    browser_installation_state["is_installed"] = False
    browser_installation_state["installation_error"] = error_msg
    print(f"тЭМ {error_msg}")
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
                print(f"ЁЯФД Trying: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env
                )
                
                if result.returncode == 0:
                    print(f"тЬЕ SUCCESS with: {cmd}")
                    if verify_browser_installation():
                        return True
                else:
                    print(f"тЭМ FAILED: {cmd}")
                    print(f"   Error: {result.stderr[:200]}...")
                    
            except Exception as e:
                print(f"ЁЯТе ERROR: {cmd} - {str(e)}")
        
        return False
        
    except Exception as e:
        print(f"ЁЯТе Critical error in python module installation: {e}")
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
            print("тЬЕ Installation script completed successfully")
            return verify_browser_installation()
        else:
            print(f"тЭМ Installation script failed: {result.stderr}")
            return False
            
    except Exception as e:
        print(f"ЁЯТе Error running installation script: {e}")
        return False

# Install browsers during startup
print("=" * 60)
print("MCQ SCRAPER - ULTRA-ROBUST VERSION")
print("=" * 60)

try:
    install_success = install_browsers_blocking()
except Exception as e:
    print(f"ЁЯЪи CRITICAL ERROR during browser installation: {e}")
    install_success = False

if not install_success:
    print("ЁЯЪи CRITICAL: Browser installation failed!")
    print("ЁЯЪи App will start but scraping functionality may be limited")
    browser_installation_state["is_installed"] = False
    browser_installation_state["installation_error"] = "Browser installation failed during startup"
else:
    print("тЬЕ Browser installation successful - Ultra-Robust App ready!")

print("=" * 60)

# Graceful shutdown handling
def handle_shutdown(signum, frame):
    """Handle graceful shutdown"""
    print("ЁЯФД Received shutdown signal, cleaning up...")
    asyncio.create_task(browser_pool.close())
    persistent_storage.save_jobs()
    print("тЬЕ Cleanup completed")

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
        
        print(f"ЁЯФС Initialized API Key Manager with {len(self.api_keys)} keys")
    
    def get_current_key(self) -> str:
        return self.api_keys[self.current_key_index]
    
    def rotate_key(self) -> Optional[str]:
        current_key = self.api_keys[self.current_key_index]
        self.exhausted_keys.add(current_key)
        print(f"тЪая╕П Key exhausted: {current_key[:20]}...")
        
        for i in range(len(self.api_keys)):
            key = self.api_keys[i]
            if key not in self.exhausted_keys:
                self.current_key_index = i
                print(f"ЁЯФД Rotated to key: {key[:20]}...")
                return key
        
        print("тЭМ All API keys exhausted!")
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
        
        # Set viewport for optimal MCQ content display
        await page.set_viewport_size({"width": 1200, "height": 800})
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
        
        # ENHANCED: Calculate central area cropping (adjusted for better left-right balance)
        # Based on 1200px viewport width, crop consistently from edges
        crop_left = 100      # REDUCED: Crop less from left edge  
        crop_top = 100       # Crop from top edge
        crop_right = 300     # INCREASED: Crop more from right edge (sidebar, ads, etc.)
        
        # Calculate screenshot dimensions for central MCQ area
        screenshot_x = crop_left
        screenshot_y = crop_top  
        screenshot_width = 1200 - crop_left - crop_right  # Central content width
        
        # ENHANCED: Calculate height to include question, options, AND answer section
        # Based on typical MCQ layout, this should capture the essential content
        base_height = 600    # Base height for question + options
        answer_section_height = 200  # Additional height for answer section
        screenshot_height = base_height + answer_section_height
        
        # Ensure we don't exceed reasonable limits
        max_height = 1000
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
        
        # Capture the screenshot with the calculated region
        try:
            screenshot = await asyncio.wait_for(
                page.screenshot(
                    clip=screenshot_region,
                    type="png"
                ),
                timeout=10.0
            )
            
            print(f"✅ MCQ screenshot captured with 67% zoom: {screenshot_width}x{screenshot_height}px")
            return screenshot
            
        except asyncio.TimeoutError:
            print(f"⏱️ Screenshot timeout for {url}")
            return None
        
    except Exception as e:
        print(f"❌ Error capturing screenshot for {url}: {str(e)}")
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
        
        # Optimized screenshot capture
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
            
            print(f"ЁЯФН Fetching results {start_index}-{start_index+9} for topic: {topic}")
            print(f"ЁЯФС Using key: {current_key[:20]}... (Remaining: {api_key_manager.get_remaining_keys()})")
            
            response = requests.get(base_url, params=params, headers=headers)
            
            if response.status_code == 429 or (response.status_code == 403 and "quota" in response.text.lower()):
                print(f"тЪая╕П Quota exceeded for current key. Attempting rotation...")
                
                next_key = api_key_manager.rotate_key()
                if next_key is None:
                    print("тЭМ All API keys exhausted!")
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
            print(f"тЬЕ Found {len(batch_links)} Testbook links in this batch. Total so far: {len(all_testbook_links)}")
            
            if len(data["items"]) < 10:
                print(f"Reached end of results with {len(data['items'])} items in last batch")
                break
            
            start_index += 10
            await asyncio.sleep(0.5)
        
        print(f"тЬЕ Total Testbook links found: {len(all_testbook_links)}")
        return all_testbook_links
        
    except Exception as e:
        print(f"тЭМ Error searching Google: {e}")
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
            print(f"тП▒я╕П Navigation timeout for {url}")
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
            print(f"тЪая╕П Error extracting question from {url}: {e}")
            return None
        
        if not question:
            print(f"тЭМ No question found on {url}")
            return None
        
        question = clean_unwanted_text(question)
        
        # Check relevance
        if not is_mcq_relevant(question, search_topic):
            print(f"тЭМ MCQ not relevant for topic '{search_topic}'")
            return None
        
        print(f"тЬЕ MCQ relevant - topic '{search_topic}' found in question body")
        
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
            print(f"тП▒я╕П Timeout extracting elements from {url}")
        
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
        print(f"тЭМ Error scraping {url}: {e}")
        return None

async def scrape_mcq_content_ultra_robust(url: str, search_topic: str) -> Optional[MCQData]:
    """Ultra-robust MCQ scraping with maximum error handling"""
    context = None
    page = None
    max_attempts = 3
    
    for attempt in range(max_attempts):
        try:
            print(f"ЁЯФН Scraping attempt {attempt + 1} for {url}")
            
            # Get context with retries
            try:
                context = await browser_pool.get_context()
            except Exception as e:
                print(f"тЪая╕П Failed to get browser context (attempt {attempt + 1}): {e}")
                if attempt == max_attempts - 1:
                    return None
                await asyncio.sleep(3)
                continue
            
            # Create page with timeout
            try:
                page = await asyncio.wait_for(context.new_page(), timeout=8.0)
            except asyncio.TimeoutError:
                print(f"тП▒я╕П Page creation timeout (attempt {attempt + 1})")
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
            print(f"тЭМ Error in scraping attempt {attempt + 1} for {url}: {e}")
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
            borderRadius=10
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
                story.append(Paragraph("📝 <b>OPTIONS:</b>", 
                    ParagraphStyle('OptionHeader', parent=styles['Normal'], 
                        fontSize=14, fontName='Helvetica-Bold', textColor=primary_color,
                        spaceAfter=10, leftIndent=20)))
                
                for j, option in enumerate(mcq.options):
                    option_letter = chr(ord('A') + j) if j < 26 else f"Option {j+1}"
                    option_text = option.replace('\n', '<br/>')
                    story.append(Paragraph(f"🔸 <b>{option_letter}.</b> {option_text}", option_style))
            
            story.append(Spacer(1, 0.2*inch))
            
            # Enhanced Answer & Solution with beautiful design
            if mcq.answer:
                story.append(Paragraph("💡 <b>ANSWER & DETAILED SOLUTION:</b>", 
                    ParagraphStyle('AnswerHeader', parent=styles['Normal'], 
                        fontSize=16, fontName='Helvetica-Bold', textColor=white,
                        backColor=success_color, borderPadding=12, alignment=TA_CENTER,
                        spaceAfter=15, borderRadius=8)))
                
                answer_text = mcq.answer.replace('\n', '<br/>')
                story.append(Paragraph(answer_text, answer_style))
            
            # Beautiful Separator between questions
            story.append(Spacer(1, 0.3*inch))
            story.append(DecorativeSeparator(doc.width, 0.1*inch))
            story.append(Spacer(1, 0.3*inch))
            
            # Strategic page breaks for better layout
            if i % 2 == 0 and i < len(mcqs):
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
        
        print(f"✅ BEAUTIFUL PROFESSIONAL PDF generated successfully: {filename} with {len(mcqs)} MCQs")
        return filename
        
    except Exception as e:
        print(f"❌ Error generating beautiful PDF: {e}")
        raise

def generate_image_based_pdf(screenshots_data: List[dict], topic: str, exam_type: str = "SSC") -> str:
    """Generate BEAUTIFUL PROFESSIONAL image-based PDF with enhanced design, graphics, and premium styling"""
    try:
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak
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
        gold_color = HexColor('#d69e2e')         # Gold for accents
        
        # 📚 ENHANCED Typography Styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Title'],
            fontSize=32,
            spaceAfter=35,
            textColor=primary_color,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold',
            borderWidth=3,
            borderColor=accent_color,
            borderPadding=20,
            backColor=light_color,
            borderRadius=10
        )
        
        header_style = ParagraphStyle(
            'CustomHeader',
            parent=styles['Heading1'],
            fontSize=18,
            spaceAfter=25,
            textColor=secondary_color,
            alignment=TA_CENTER,
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
            fontSize=16,
            spaceAfter=15,
            fontName='Helvetica-Bold',
            textColor=white,
            borderWidth=2,
            borderColor=primary_color,
            borderPadding=12,
            backColor=primary_color,
            alignment=TA_CENTER,
            borderRadius=8
        )
        
        # 📋 STUNNING COVER PAGE with Graphics
        story.append(DecorativeSeparator(doc.width, 0.2*inch))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(Paragraph("🎓 PREMIUM MCQ COLLECTION", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph(f"📚 Subject: {topic.upper()}", header_style))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(Paragraph("📸 Format: Enhanced Screenshots", header_style))
        story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph(f"🎯 Total Questions: {len(screenshots_data)}", header_style))
        story.append(Spacer(1, 0.2*inch))
        
        story.append(Paragraph(f"📅 Generated on: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}", header_style))
        story.append(Spacer(1, 0.3*inch))
        
        story.append(DecorativeSeparator(doc.width, 0.15*inch))
        story.append(PageBreak())
        
        # 📝 ENHANCED MCQ SCREENSHOTS with Beautiful Styling
        for i, screenshot_item in enumerate(screenshots_data, 1):
            # Professional Question header with graphics
            story.append(Paragraph(f"🎯 QUESTION {i} OF {len(screenshots_data)} 🎯", question_header_style))
            story.append(Spacer(1, 0.2*inch))
            
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
        update_job_progress(job_id, "running", f"ЁЯФН Searching for {exam_type} '{topic}' results with ultra-smart filtering...")
        
        # Search for links
        links = await search_google_custom(topic, exam_type)
        
        if not links:
            update_job_progress(job_id, "completed", f"тЭМ No {exam_type} results found for '{topic}'. Please try another topic.", 
                              total_links=0, processed_links=0, mcqs_found=0)
            return
        
        update_job_progress(job_id, "running", f"тЬЕ Found {len(links)} {exam_type} links. Starting ultra-smart filtering extraction...", 
                          total_links=len(links))
        
        if pdf_format == "image":
            await process_screenshot_extraction_ultra_robust(job_id, topic, exam_type, links)
        else:
            await process_text_extraction_ultra_robust(job_id, topic, exam_type, links)
        
    except Exception as e:
        error_message = str(e)
        print(f"тЭМ Critical error in process_mcq_extraction: {e}")
        update_job_progress(job_id, "error", f"тЭМ Error: {error_message}")

async def process_text_extraction_ultra_robust(job_id: str, topic: str, exam_type: str, links: List[str]):
    """Ultra-robust text extraction with persistent job tracking"""
    try:
        await browser_pool.initialize()
        
        mcqs = []
        relevant_mcqs = 0
        irrelevant_mcqs = 0
        
        print(f"ЁЯЪА Starting ULTRA-ROBUST text processing: {len(links)} links")
        
        for i, url in enumerate(links):
            print(f"ЁЯФН Processing link {i + 1}/{len(links)}: {url}")
            
            # Update progress with persistent storage
            current_progress = f"ЁЯФН Processing link {i + 1}/{len(links)} - Ultra-smart filtering enabled..."
            update_job_progress(job_id, "running", current_progress, 
                              processed_links=i, mcqs_found=len(mcqs))
            
            try:
                result = await scrape_mcq_content_ultra_robust(url, topic)
                
                if result:
                    mcqs.append(result)
                    relevant_mcqs += 1
                    print(f"тЬЕ Found relevant MCQ {i + 1}/{len(links)} - Total: {len(mcqs)}")
                else:
                    irrelevant_mcqs += 1
                    print(f"тЪая╕П Skipped irrelevant MCQ {i + 1}/{len(links)}")
                    
            except Exception as e:
                print(f"тЭМ Error processing link {i + 1}: {e}")
                irrelevant_mcqs += 1
            
            # Update progress
            update_job_progress(job_id, "running", 
                              f"тЬЕ Processed {i + 1}/{len(links)} links - Found {len(mcqs)} relevant MCQs", 
                              processed_links=i + 1, mcqs_found=len(mcqs))
            
            # Small delay
            if i < len(links) - 1:
                await asyncio.sleep(1)
        
        await browser_pool.close()
        
        if not mcqs:
            update_job_progress(job_id, "completed", 
                              f"тЭМ No relevant MCQs found for '{topic}' across {len(links)} links.", 
                              total_links=len(links), processed_links=len(links), mcqs_found=0)
            return
        
        # Generate PDF
        final_message = f"тЬЕ Ultra-smart filtering complete! Found {relevant_mcqs} relevant MCQs from {len(links)} total links."
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
            
            success_message = f"ЁЯОЙ SUCCESS! Generated PDF with {len(mcqs)} relevant MCQs for topic '{topic}'."
            update_job_progress(job_id, "completed", success_message, 
                              total_links=len(links), processed_links=len(links), 
                              mcqs_found=len(mcqs), pdf_url=pdf_url)
            
            print(f"тЬЕ Job {job_id} completed successfully with {len(mcqs)} MCQs")
            
        except Exception as e:
            print(f"тЭМ Error generating PDF: {e}")
            update_job_progress(job_id, "error", f"тЭМ Error generating PDF: {str(e)}")
    
    except Exception as e:
        print(f"тЭМ Critical error in text extraction: {e}")
        update_job_progress(job_id, "error", f"тЭМ Critical error: {str(e)}")
        await browser_pool.close()

async def process_screenshot_extraction_ultra_robust(job_id: str, topic: str, exam_type: str, links: List[str]):
    """
    Optimized screenshot extraction with faster processing and smart context reuse
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
                result = await scrape_testbook_page_with_screenshot_ultra_robust(context, url, topic)
                
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
                              f"❌ No relevant screenshots captured for '{topic}'.", 
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
            
            success_message = f"🎉 SUCCESS! Generated optimized image-based PDF with {len(screenshot_data)} relevant screenshots for topic '{topic}'."
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

# Enhanced API Routes
@app.get("/api/health")
async def health_check():
    """Enhanced health check with system status"""
    return {
        "status": "healthy",
        "message": "Ultra-Robust MCQ Scraper API is running",
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
        f"ЁЯЪА Starting ultra-robust {request.exam_type} MCQ extraction for '{request.topic}' ({request.pdf_format} format)..."
    )
    
    # Start background task
    background_tasks.add_task(
        process_mcq_extraction,
        job_id=job_id,
        topic=request.topic.strip(),
        exam_type=request.exam_type,
        pdf_format=request.pdf_format
    )
    
    return {
        "job_id": job_id,
        "status": "running",
        "message": f"Started ultra-robust {request.exam_type} MCQ extraction for '{request.topic}' ({request.pdf_format} format)",
        "progress": f"ЁЯЪА Starting ultra-robust {request.exam_type} MCQ extraction for '{request.topic}' ({request.pdf_format} format)..."
    }

@app.get("/api/job-status/{job_id}")
async def get_job_status(job_id: str):
    """
    Enhanced job status with smart connection monitoring and unicode fix
    """
    try:
        job_data = persistent_storage.get_job(job_id)
        
        if not job_data:
            raise HTTPException(status_code=404, detail="Job not found")
        
        # Unicode/Emoji Fix - Ensure proper UTF-8 encoding for all text fields
        safe_job_data = {}
        for key, value in job_data.items():
            if isinstance(value, str):
                # Replace problematic unicode characters with proper text equivalents
                safe_value = value.encode('utf-8', errors='ignore').decode('utf-8')
                # Replace common emoji patterns with text equivalents
                emoji_replacements = {
                    'ЁЯЪА': '[STARTING]',
                    'ЁЯУК': '[STATUS]',
                    'тЬЕ': '[SUCCESS]',
                    'тЭМ': '[ERROR]',
                    'тП▒я╕П': '[TIMEOUT]',
                    'тЪая╕П': '[WARNING]',
                    'ЁЯФН': '[PROCESSING]',
                    'ЁЯУ╕': '[SCREENSHOT]',
                    'ЁЯЦ╝я╕П': '[IMAGE]',
                    'ЁЯУЪ': '[PDF]',
                    'ЁЯФД': '[RETRY]',
                    'ЁЯУЭ': '[FOUND]',
                    'ЁЯУП': '[DIMENSIONS]',
                    'ЁЯФН': '[SEARCH]',
                    'ЁЯМР': '[WEB]',
                    'тнР': '[COMPLETE]',
                    'ЁЯОЙ': '[DONE]',
                    'ЁЯТб': '[INFO]',
                    'ЁЯФЗ': '[PROCESSING]',
                    'ЁЯЪй': '[FLAG]',
                    'ЁЯУИ': '[PROGRESS]',
                    'ЁЯОк': '[MCQ]',
                    'ЁЯТ╛': '[SAVED]'
                }
                
                for emoji, replacement in emoji_replacements.items():
                    safe_value = safe_value.replace(emoji, replacement)
                
                safe_job_data[key] = safe_value
            else:
                safe_job_data[key] = value
        
        # Add browser monitoring status to response
        browser_status = {
            "browser_active": browser_pool.is_initialized,
            "browser_restart_count": browser_pool.restart_count,
            "last_browser_error": browser_pool.last_error
        }
        safe_job_data["browser_monitoring"] = browser_status
        
        # Add connection health indicator
        safe_job_data["connection_health"] = "stable"
        
        # If job is running, check if browser is actually working
        if safe_job_data.get("status") == "running":
            if not browser_pool.is_initialized:
                safe_job_data["connection_health"] = "browser_restarting"
                if "STARTING" not in safe_job_data.get("progress", ""):
                    safe_job_data["progress"] = "[PROCESSING] Browser restarting, continuing processing..."
        
        print(f"[STATUS] Returning enhanced status for job {job_id}: {safe_job_data.get('status')} - {safe_job_data.get('progress', '')[:100]}...")
        return safe_job_data
        
    except HTTPException:
        raise
    except Exception as e:
        error_message = str(e).encode('utf-8', errors='ignore').decode('utf-8')
        print(f"[ERROR] Error getting job status for {job_id}: {error_message}")
        return {
            "job_id": job_id,
            "status": "error",
            "progress": f"Error retrieving job status: {error_message}",
            "total_links": 0,
            "processed_links": 0,
            "mcqs_found": 0,
            "pdf_url": None,
            "connection_health": "error",
            "browser_monitoring": {
                "browser_active": False,
                "browser_restart_count": 0,
                "last_browser_error": error_message
            }
        }

@app.get("/api/browser-status")
async def get_browser_status():
    """
    Browser monitoring endpoint for smart connection management
    """
    try:
        return {
            "browser_installed": browser_installation_state["is_installed"],
            "browser_active": browser_pool.is_initialized,
            "browser_restart_count": browser_pool.restart_count,
            "last_browser_error": browser_pool.last_error,
            "installation_attempted": browser_installation_state["installation_attempted"],
            "installation_in_progress": browser_installation_state["installation_in_progress"],
            "timestamp": datetime.now().isoformat(),
            "connection_health": "stable" if browser_pool.is_initialized else "restarting"
        }
    except Exception as e:
        error_message = str(e).encode('utf-8', errors='ignore').decode('utf-8')
        return {
            "browser_installed": False,
            "browser_active": False,
            "browser_restart_count": 0,
            "last_browser_error": error_message,
            "installation_attempted": False,
            "installation_in_progress": False,
            "timestamp": datetime.now().isoformat(),
            "connection_health": "error"
        }

@app.get("/api/download-pdf/{filename}")
async def download_pdf(filename: str):
    """Download generated PDF file with environment-aware path resolution"""
    pdf_dir = get_pdf_directory()
    filepath = pdf_dir / filename
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="PDF file not found")
    
    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type='application/pdf'
    )

@app.get("/api/jobs")
async def get_all_jobs():
    """Get all active jobs"""
    try:
        return {
            "jobs": list(persistent_storage.jobs.values()),
            "total_jobs": len(persistent_storage.jobs)
        }
    except Exception as e:
        print(f"тЭМ Error getting all jobs: {e}")
        return {"jobs": [], "total_jobs": 0}

# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize ultra-robust services on startup"""
    print("ЁЯЪА Ultra-Robust MCQ Scraper API starting up...")
    print(f"ЁЯУК Browser installation status: {browser_installation_state}")
    print(f"ЁЯУВ Active jobs loaded: {len(persistent_storage.jobs)}")
    
    # Clean up old jobs
    persistent_storage.cleanup_old_jobs()

# Shutdown event
@app.on_event("shutdown")
async def shutdown_event():
    """Enhanced cleanup on shutdown"""
    print("ЁЯФД Ultra-Robust MCQ Scraper API shutting down...")
    try:
        # Save jobs before shutdown
        persistent_storage.save_jobs()
        print("ЁЯТ╛ Jobs saved to persistent storage")
        
        # Close browser pool
        await browser_pool.close()
        print("тЬЕ Browser pool closed successfully")
        
    except Exception as e:
        print(f"тЪая╕П Error during shutdown: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
