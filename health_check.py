#!/usr/bin/env python3
"""
Health check script for Playwright installation
"""

import os
import sys
import asyncio
from pathlib import Path

def check_browser_files():
    """Check if browser files exist"""
    browser_path = Path("/tmp/pw-browsers")
    
    if not browser_path.exists():
        print("❌ Browser directory does not exist")
        return False
    
    # Look for chromium directories
    chromium_dirs = list(browser_path.glob("chromium*"))
    
    if not chromium_dirs:
        print("❌ No chromium directories found")
        return False
    
    print(f"✅ Found {len(chromium_dirs)} chromium directories")
    for dir_path in chromium_dirs:
        print(f"  - {dir_path.name}")
    
    return True

async def test_playwright_import():
    """Test if Playwright can be imported and used"""
    try:
        from playwright.async_api import async_playwright
        print("✅ Playwright import successful")
        
        # Set environment
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/tmp/pw-browsers'
        
        # Try to create a browser instance
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto("data:text/html,<h1>Test</h1>")
            title = await page.title()
            await browser.close()
            
            print(f"✅ Playwright functionality test passed")
            return True
            
    except Exception as e:
        print(f"❌ Playwright test failed: {e}")
        return False

def main():
    """Main health check"""
    print("=" * 50)
    print("PLAYWRIGHT HEALTH CHECK")
    print("=" * 50)
    
    # Check 1: Browser files
    files_ok = check_browser_files()
    
    # Check 2: Playwright functionality
    try:
        functionality_ok = asyncio.run(test_playwright_import())
    except Exception as e:
        print(f"❌ Async test failed: {e}")
        functionality_ok = False
    
    print("\n" + "=" * 50)
    if files_ok and functionality_ok:
        print("✅ ALL CHECKS PASSED - Playwright is ready!")
        sys.exit(0)
    else:
        print("❌ SOME CHECKS FAILED - Playwright may not work properly")
        sys.exit(1)

if __name__ == "__main__":
    main()