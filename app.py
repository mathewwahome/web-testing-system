import asyncio
import aiohttp
import json
import time
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import sqlite3
from flask import Flask, render_template, request, jsonify, send_from_directory
import threading
import os
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Set
import re

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class TestResult:
    url: str
    status_code: int
    response_time: float
    error: Optional[str]
    timestamp: datetime
    screenshot_path: Optional[str] = None
    page_title: str = ""
    forms_found: int = 0
    links_found: int = 0

@dataclass
class CrawlResult:
    domain: str
    total_urls: int
    crawled_urls: int
    failed_urls: int
    test_results: List[TestResult]
    start_time: datetime
    end_time: Optional[datetime] = None

class WebCrawler:
    def __init__(self, domain: str, max_depth: int = 3, max_urls: int = 100):
        self.domain = domain
        self.base_url = f"https://{domain}" if not domain.startswith(('http://', 'https://')) else domain
        self.max_depth = max_depth
        self.max_urls = max_urls
        self.visited_urls: Set[str] = set()
        self.found_urls: Set[str] = set()
        self.session = None
        
    async def init_session(self):
        """Initialize aiohttp session"""
        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(ssl=False)  # Allow insecure SSL for testing
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        
    async def close_session(self):
        """Close aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    def is_valid_url(self, url: str) -> bool:
        """Check if URL belongs to the target domain"""
        try:
            parsed = urlparse(url)
            base_parsed = urlparse(self.base_url)
            return parsed.netloc == base_parsed.netloc
        except Exception:
            return False
    
    async def fetch_page(self, url: str) -> Optional[str]:
        """Fetch page content"""
        try:
            async with self.session.get(url, allow_redirects=True) as response:
                if response.status == 200:
                    return await response.text()
                else:
                    logger.warning(f"HTTP {response.status} for {url}")
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
        return None
    
    def extract_links(self, html: str, base_url: str) -> Set[str]:
        """Extract all links from HTML"""
        soup = BeautifulSoup(html, 'html.parser')
        links = set()
        
        for tag in soup.find_all(['a', 'link'], href=True):
            href = tag['href']
            if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                continue
                
            full_url = urljoin(base_url, href)
            
            # Clean URL (remove fragments and query params for crawling)
            parsed = urlparse(full_url)
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            
            if self.is_valid_url(clean_url) and clean_url not in self.visited_urls:
                links.add(clean_url)
        
        # Also extract form actions
        for form in soup.find_all('form', action=True):
            action = form['action']
            if action:
                full_url = urljoin(base_url, action)
                parsed = urlparse(full_url)
                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                if self.is_valid_url(clean_url):
                    links.add(clean_url)
                
        return links
    
    async def crawl(self) -> Set[str]:
        """Crawl the website and return all found URLs"""
        await self.init_session()
        
        try:
            urls_to_visit = [(self.base_url, 0)]
            
            while urls_to_visit and len(self.found_urls) < self.max_urls:
                current_url, depth = urls_to_visit.pop(0)
                
                if current_url in self.visited_urls or depth > self.max_depth:
                    continue
                
                logger.info(f"Crawling: {current_url} (depth: {depth})")
                self.visited_urls.add(current_url)
                self.found_urls.add(current_url)
                
                html = await self.fetch_page(current_url)
                if html:
                    links = self.extract_links(html, current_url)
                    for link in links:
                        if link not in self.visited_urls and len(self.found_urls) < self.max_urls:
                            urls_to_visit.append((link, depth + 1))
            
        except Exception as e:
            logger.error(f"Crawling error: {e}")
        finally:
            await self.close_session()
        
        return self.found_urls


class WebTester:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = None
        self.screenshots_dir = "screenshots"
        os.makedirs(self.screenshots_dir, exist_ok=True)
    
    def setup_driver(self):
        """Setup Selenium WebDriver"""
        chrome_options = Options()
        if self.headless:
            chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-web-security")
        chrome_options.add_argument("--allow-running-insecure-content")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_argument("--ignore-ssl-errors")
        chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        
        try:
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.implicitly_wait(10)
            self.driver.set_page_load_timeout(30)
        except Exception as e:
            logger.error(f"Failed to setup Chrome driver: {e}")
            raise
    
    def close_driver(self):
        """Close WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.error(f"Error closing driver: {e}")
            finally:
                self.driver = None
    
    def login(self, login_url: str, username: str, password: str, 
              username_selector: str = "input[name='username'], input[name='email'], #username, #email",
              password_selector: str = "input[name='password'], #password",
              submit_selector: str = "input[type='submit'], button[type='submit'], #login-button, .login-btn"):
        """Attempt to login to the website"""
        try:
            logger.info(f"Attempting login at: {login_url}")
            self.driver.get(login_url)
            
            # Wait for page to load
            WebDriverWait(self.driver, 10).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
            
            # Find and fill username
            username_field = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, username_selector))
            )
            username_field.clear()
            username_field.send_keys(username)
            
            # Find and fill password
            password_field = self.driver.find_element(By.CSS_SELECTOR, password_selector)
            password_field.clear()
            password_field.send_keys(password)
            
            # Submit form
            submit_button = self.driver.find_element(By.CSS_SELECTOR, submit_selector)
            submit_button.click()
            
            # Wait for login to complete
            WebDriverWait(self.driver, 15).until(
                lambda driver: driver.current_url != login_url
            )
            
            logger.info("Login successful")
            return True
            
        except Exception as e:
            logger.error(f"Login failed: {e}")
            return False
    
    def test_url(self, url: str) -> TestResult:
        """Test a single URL"""
        start_time = time.time()
        screenshot_path = None
        
        try:
            logger.info(f"Testing URL: {url}")
            self.driver.get(url)
            
            # Wait for page to load
            WebDriverWait(self.driver, 20).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )
            
            response_time = time.time() - start_time
            
            # Get page information
            page_title = self.driver.title or "No Title"
            forms = self.driver.find_elements(By.TAG_NAME, "form")
            links = self.driver.find_elements(By.TAG_NAME, "a")
            
            # Check for JavaScript errors (only severe ones)
            error_msg = None
            try:
                logs = self.driver.get_log('browser')
                js_errors = [log for log in logs if log['level'] == 'SEVERE']
                if js_errors:
                    error_msg = f"JavaScript errors found: {len(js_errors)} severe errors"
            except Exception:
                # Browser logs might not be available in headless mode
                pass
            
            # Take screenshot
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                url_safe = re.sub(r'[^\w\-_\.]', '_', urlparse(url).path)[:50]
                screenshot_filename = f"screenshot_{timestamp}_{url_safe}.png"
                screenshot_path = os.path.join(self.screenshots_dir, screenshot_filename)
                self.driver.save_screenshot(screenshot_path)
            except Exception as e:
                logger.warning(f"Failed to take screenshot for {url}: {e}")
            
            return TestResult(
                url=url,
                status_code=200,  # Assume 200 if page loaded
                response_time=response_time,
                error=error_msg,
                timestamp=datetime.now(),
                screenshot_path=screenshot_path,
                page_title=page_title,
                forms_found=len(forms),
                links_found=len(links)
            )
            
        except TimeoutException:
            return TestResult(
                url=url,
                status_code=408,
                response_time=time.time() - start_time,
                error="Page load timeout",
                timestamp=datetime.now(),
                screenshot_path=screenshot_path
            )
        except WebDriverException as e:
            return TestResult(
                url=url,
                status_code=500,
                response_time=time.time() - start_time,
                error=f"WebDriver error: {str(e)[:200]}",
                timestamp=datetime.now(),
                screenshot_path=screenshot_path
            )
        except Exception as e:
            return TestResult(
                url=url,
                status_code=500,
                response_time=time.time() - start_time,
                error=f"Unexpected error: {str(e)[:200]}",
                timestamp=datetime.now(),
                screenshot_path=screenshot_path
            )


class DatabaseManager:
    def __init__(self):
        # Create data directory if it doesn't exist
        self.data_dir = 'data'
        os.makedirs(self.data_dir, exist_ok=True)
        
        # Use data directory for database
        self.db_path = os.path.join(self.data_dir, 'test_results.db')
        self.init_database()
    
    def init_database(self):
        """Initialize the database with required tables"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Create test_results table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status_code INTEGER,
                    response_time REAL,
                    error TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    screenshot_path TEXT,
                    page_title TEXT,
                    forms_found INTEGER DEFAULT 0,
                    links_found INTEGER DEFAULT 0
                )
            ''')
            
            # Create crawl_sessions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS crawl_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    total_urls INTEGER,
                    crawled_urls INTEGER,
                    failed_urls INTEGER,
                    start_time DATETIME,
                    end_time DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.commit()
            conn.close()
            logger.info(f"Database initialized successfully at {self.db_path}")
            
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise
    
    def save_test_result(self, result: TestResult, domain: str):
        """Save a test result to the database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO test_results (
                    url, domain, status_code, response_time, error,
                    timestamp, screenshot_path, page_title, forms_found, links_found
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                result.url, domain, result.status_code, result.response_time,
                result.error, result.timestamp, result.screenshot_path,
                result.page_title, result.forms_found, result.links_found
            ))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error saving test result: {e}")
    
    def save_crawl_session(self, crawl_result: CrawlResult):
        """Save a crawl session to the database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO crawl_sessions (
                    domain, total_urls, crawled_urls, failed_urls,
                    start_time, end_time
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                crawl_result.domain, crawl_result.total_urls,
                crawl_result.crawled_urls, crawl_result.failed_urls,
                crawl_result.start_time, crawl_result.end_time
            ))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error saving crawl session: {e}")
    
    def get_test_results(self, domain: str = None, limit: int = 100):
        """Get test results from the database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if domain:
                cursor.execute('''
                    SELECT * FROM test_results 
                    WHERE domain = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (domain, limit))
            else:
                cursor.execute('''
                    SELECT * FROM test_results 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (limit,))
            
            results = cursor.fetchall()
            conn.close()
            
            # Convert to list of dictionaries
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in results]
            
        except Exception as e:
            logger.error(f"Error getting test results: {e}")
            return []
        
        
class WebTestingSystem:
    def __init__(self):
        self.db = DatabaseManager()
        self.current_test = None
        self.test_thread = None
    
    async def run_full_test(self, domain: str, login_url: str = None, 
                           username: str = None, password: str = None,
                           max_depth: int = 3, max_urls: int = 50) -> CrawlResult:
        """Run complete crawl and test cycle"""
        start_time = datetime.now()
        logger.info(f"Starting full test for domain: {domain}")
        
        # Crawl the website
        crawler = WebCrawler(domain, max_depth, max_urls)
        found_urls = await crawler.crawl()
        
        logger.info(f"Found {len(found_urls)} URLs to test")
        
        if not found_urls:
            logger.warning("No URLs found during crawling")
            return CrawlResult(
                domain=domain,
                total_urls=0,
                crawled_urls=0,
                failed_urls=0,
                test_results=[],
                start_time=start_time,
                end_time=datetime.now()
            )
        
        # Test each URL
        tester = WebTester(headless=True)
        test_results = []
        failed_count = 0
        
        try:
            tester.setup_driver()
            
            # Login if credentials provided
            if login_url and username and password:
                if not tester.login(login_url, username, password):
                    logger.warning("Login failed, continuing without authentication")
            
            for i, url in enumerate(found_urls, 1):
                logger.info(f"Testing URL {i}/{len(found_urls)}: {url}")
                result = tester.test_url(url)
                test_results.append(result)
                
                # Save result to database
                self.db.save_test_result(result, domain)
                
                if result.error:
                    failed_count += 1
                    logger.warning(f"Test failed for {url}: {result.error}")
                
                # Small delay between tests to avoid overwhelming the server
                time.sleep(2)
        
        except Exception as e:
            logger.error(f"Error during testing: {e}")
        finally:
            tester.close_driver()
        
        end_time = datetime.now()
        
        crawl_result = CrawlResult(
            domain=domain,
            total_urls=len(found_urls),
            crawled_urls=len(test_results),
            failed_urls=failed_count,
            test_results=test_results,
            start_time=start_time,
            end_time=end_time
        )
        
        # Save crawl session
        self.db.save_crawl_session(crawl_result)
        
        logger.info(f"Test completed. {len(test_results)} URLs tested, {failed_count} failed")
        return crawl_result


# Flask Web Application
app = Flask(__name__)
testing_system = WebTestingSystem()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_test', methods=['POST'])
def start_test():
    data = request.json
    domain = data.get('domain')
    login_url = data.get('login_url')
    username = data.get('username')
    password = data.get('password')
    max_depth = data.get('max_depth', 3)
    max_urls = data.get('max_urls', 50)
    
    if not domain:
        return jsonify({'error': 'Domain is required'}), 400
    
    def run_test():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                testing_system.run_full_test(
                    domain, login_url, username, password, max_depth, max_urls
                )
            )
            testing_system.current_test = result
        except Exception as e:
            logger.error(f"Test failed: {e}")
            testing_system.current_test = None
        finally:
            loop.close()
    
    if testing_system.test_thread and testing_system.test_thread.is_alive():
        return jsonify({'error': 'Test already running'}), 400
    
    testing_system.test_thread = threading.Thread(target=run_test)
    testing_system.test_thread.start()
    
    return jsonify({'message': 'Test started successfully'})

@app.route('/test_status')
def test_status():
    if testing_system.test_thread and testing_system.test_thread.is_alive():
        return jsonify({'status': 'running'})
    elif testing_system.current_test:
        # Convert datetime objects to strings for JSON serialization
        result_dict = asdict(testing_system.current_test)
        result_dict['start_time'] = testing_system.current_test.start_time.isoformat()
        if testing_system.current_test.end_time:
            result_dict['end_time'] = testing_system.current_test.end_time.isoformat()
        
        # Convert test results datetime objects
        for test_result in result_dict['test_results']:
            test_result['timestamp'] = test_result['timestamp'].isoformat() if isinstance(test_result['timestamp'], datetime) else str(test_result['timestamp'])
        
        return jsonify({
            'status': 'completed',
            'result': result_dict
        })
    else:
        return jsonify({'status': 'idle'})

@app.route('/results')
def get_results():
    domain = request.args.get('domain')
    limit = int(request.args.get('limit', 100))
    results = testing_system.db.get_test_results(domain, limit)
    return jsonify(results)

@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    return send_from_directory('screenshots', filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)