#!/usr/bin/env python3
"""
Playwright Installation Script for Persistent Browser Setup
This script ensures Playwright browsers are always installed and available
"""

import os
import subprocess
import sys
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/playwright_install.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

def run_command(command, timeout=300):
    """Run a command with proper error handling and logging"""
    try:
        logger.info(f"Running command: {command}")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        if result.returncode == 0:
            logger.info(f"Command succeeded: {command}")
            if result.stdout:
                logger.info(f"stdout: {result.stdout}")
            return True
        else:
            logger.error(f"Command failed: {command}")
            logger.error(f"Return code: {result.returncode}")
            logger.error(f"stderr: {result.stderr}")
            if result.stdout:
                logger.error(f"stdout: {result.stdout}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out: {command}")
        return False
    except Exception as e:
        logger.error(f"Error running command '{command}': {e}")
        return False

def check_playwright_installed():
    """Check if Playwright Python package is installed"""
    try:
        import playwright
        logger.info("Playwright Python package is installed")
        return True
    except ImportError:
        logger.error("Playwright Python package is not installed")
        return False

def check_browsers_installed():
    """Check if Playwright browsers are installed"""
    try:
        # Use dynamic approach to check for any installed browsers
        browser_base_path = "/tmp/pw-browsers"
        
        if not os.path.exists(browser_base_path):
            logger.warning(f"Browser base path {browser_base_path} does not exist")
            return False
        
        # Look for any chromium installation (different versions)
        chromium_dirs = []
        chromium_headless_dirs = []
        
        for item in os.listdir(browser_base_path):
            item_path = os.path.join(browser_base_path, item)
            if os.path.isdir(item_path):
                if item.startswith("chromium-"):
                    chromium_dirs.append(item_path)
                elif item.startswith("chromium_headless_shell-"):
                    chromium_headless_dirs.append(item_path)
        
        logger.info(f"Found chromium directories: {chromium_dirs}")
        logger.info(f"Found chromium headless directories: {chromium_headless_dirs}")
        
        # Check if we have at least one chromium installation
        if chromium_dirs or chromium_headless_dirs:
            logger.info("Chromium browser installation found")
            return True
        else:
            logger.warning("No chromium browser installation found")
            return False
            
    except Exception as e:
        logger.error(f"Error checking browser installation: {e}")
        return False

def install_playwright_browsers():
    """Install Playwright browsers with dependencies"""
    logger.info("Starting Playwright browser installation...")
    
    # Ensure we have the right environment
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = "/tmp/pw-browsers"
    
    # Install system dependencies first
    install_system_dependencies()
    
    # First, ensure we have npx and Node.js installed
    logger.info("Ensuring Node.js and npx are available...")
    run_command("curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -", timeout=300)
    run_command("apt-get install -y nodejs", timeout=300)
    
    # Try different installation approaches
    installation_commands = [
        "npx playwright install --with-deps chromium",
        "npx playwright install chromium",
        "npx -y playwright install --with-deps chromium",
        "npx -y playwright install chromium"
    ]
    
    for cmd in installation_commands:
        logger.info(f"Attempting installation with: {cmd}")
        success = run_command(cmd, timeout=600)
        
        if success:
            logger.info(f"Playwright browsers installed successfully with: {cmd}")
            
            # Verify installation worked
            if check_browsers_installed():
                logger.info("Browser installation verification passed")
                return True
            else:
                logger.warning("Browser installation verification failed, trying next command...")
        else:
            logger.warning(f"Installation failed with: {cmd}")
    
    logger.error("All installation attempts failed")
    return False

def install_system_dependencies():
    """Install system dependencies for Playwright browsers"""
    logger.info("Installing system dependencies for Playwright...")
    
    # Install system dependencies that Playwright browsers need
    dependencies = [
        "apt-get update",
        "apt-get install -y libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgtk-3-0 libgbm1 libasound2"
    ]
    
    for dep_command in dependencies:
        success = run_command(dep_command, timeout=180)
        if not success:
            logger.warning(f"Failed to install system dependency: {dep_command}")
            # Continue with other dependencies even if one fails
    
    logger.info("System dependencies installation completed")

def verify_playwright_functionality():
    """Verify that Playwright can actually launch browsers"""
    logger.info("Verifying Playwright functionality...")
    
    # Create a simple test script to verify Playwright works
    test_script = """
import asyncio
import os
from playwright.async_api import async_playwright

async def test_playwright():
    try:
        # Set the browser path environment variable
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                # Let Playwright find the browser automatically
                executable_path=None
            )
            page = await browser.new_page()
            await page.goto('https://example.com', timeout=10000)
            title = await page.title()
            await browser.close()
            print(f"SUCCESS: Page title is '{title}'")
            return True
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    result = asyncio.run(test_playwright())
    exit(0 if result else 1)
"""
    
    # Write test script to temporary file
    test_file = "/tmp/test_playwright.py"
    with open(test_file, "w") as f:
        f.write(test_script)
    
    # Run the test with proper environment
    env = os.environ.copy()
    env['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
    
    try:
        result = subprocess.run(
            [sys.executable, test_file],
            capture_output=True,
            text=True,
            timeout=60,
            env=env
        )
        
        logger.info(f"Test result stdout: {result.stdout}")
        if result.stderr:
            logger.info(f"Test result stderr: {result.stderr}")
        
        success = result.returncode == 0
        
    except subprocess.TimeoutExpired:
        logger.error("Playwright functionality test timed out")
        success = False
    except Exception as e:
        logger.error(f"Error running functionality test: {e}")
        success = False
    
    # Clean up
    if os.path.exists(test_file):
        os.remove(test_file)
    
    if success:
        logger.info("Playwright functionality verification passed")
        return True
    else:
        logger.error("Playwright functionality verification failed")
        return False

def main():
    """Main installation and verification process"""
    logger.info("=" * 60)
    logger.info("PLAYWRIGHT INSTALLATION SCRIPT STARTED")
    logger.info("=" * 60)
    
    # Step 1: Check if Playwright Python package is installed
    if not check_playwright_installed():
        logger.error("Playwright Python package is not installed. Please install it first.")
        sys.exit(1)
    
    # Step 2: Check if browsers are already installed
    if check_browsers_installed():
        logger.info("Playwright browsers are already installed")
        
        # Verify functionality
        if verify_playwright_functionality():
            logger.info("Playwright is working correctly - installation not needed")
            sys.exit(0)
        else:
            logger.warning("Playwright browsers exist but functionality test failed - reinstalling...")
    
    # Step 3: Install system dependencies
    install_system_dependencies()
    
    # Step 4: Install Playwright browsers
    if not install_playwright_browsers():
        logger.error("Failed to install Playwright browsers")
        sys.exit(1)
    
    # Step 5: Verify installation
    if not check_browsers_installed():
        logger.error("Browser installation verification failed")
        sys.exit(1)
    
    # Step 6: Verify functionality
    if not verify_playwright_functionality():
        logger.error("Playwright functionality verification failed")
        sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("PLAYWRIGHT INSTALLATION COMPLETED SUCCESSFULLY")
    logger.info("=" * 60)
    
    sys.exit(0)

if __name__ == "__main__":
    main()
