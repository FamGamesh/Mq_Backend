#!/usr/bin/env python3
"""
Post-deployment Playwright setup script for Render and other cloud platforms.
This script can be run after deployment to ensure browsers are installed.
"""

import os
import sys
import subprocess
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def install_playwright_browsers():
    """Install Playwright browsers in cloud environment"""
    logger.info("Starting Playwright browser installation for cloud deployment...")
    
    try:
        # Set environment variables
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
        os.environ['PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'] = '0'
        
        # Create browser directory
        os.makedirs("/tmp/pw-browsers", exist_ok=True)
        
        # Install browsers using Python module
        commands = [
            f"{sys.executable} -m playwright install chromium --with-deps",
            f"{sys.executable} -m playwright install chromium"
        ]
        
        for cmd in commands:
            try:
                logger.info(f"Executing: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=600
                )
                
                if result.returncode == 0:
                    logger.info(f"✅ SUCCESS: {cmd}")
                    logger.info(f"Output: {result.stdout}")
                    
                    # Verify installation
                    if os.path.exists("/tmp/pw-browsers"):
                        logger.info("✅ Browser installation verified")
                        return True
                else:
                    logger.error(f"❌ FAILED: {cmd}")
                    logger.error(f"Error: {result.stderr}")
                    
            except Exception as e:
                logger.error(f"Exception running {cmd}: {e}")
        
        logger.error("All installation attempts failed")
        return False
        
    except Exception as e:
        logger.error(f"Critical error in browser installation: {e}")
        return False

def main():
    """Main setup function"""
    logger.info("=" * 50)
    logger.info("POST-DEPLOYMENT PLAYWRIGHT SETUP")
    logger.info("=" * 50)
    
    success = install_playwright_browsers()
    
    if success:
        logger.info("✅ Playwright setup completed successfully!")
        sys.exit(0)
    else:
        logger.error("❌ Playwright setup failed!")
        sys.exit(1)

if __name__ == "__main__":
    main()