#!/usr/bin/python3
# -*- encoding: utf-8 -*-
"""
    @Description: Core of NoXss, include preprocess, detect, scan, etc.

    ~~~~~~
    @Author  : longwenzhang
    @Time    : 19-10-9  10:13
"""

import pickle
import os
import time
import urllib.request as urllib
import urllib.parse as urllib2
from queue import Empty
from http.client import BadStatusLine, InvalidURL
from multiprocessing import Process, Manager
import json
import re
from log import LOGGER
import urllib.parse as urlparse
from ssl import CertificateError
from xml.etree import cElementTree
from selenium.common.exceptions import TimeoutException, UnexpectedAlertPresentException
from config import TRAFFIC_DIR, REQUEST_ERROR, REDIRECT, MULTIPART
from cookie import get_cookie
from model import Case, HttpRequest, HttpResponse
from util import functimeout, Func_timeout_error, change_by_param, list2dict, chrome, phantomjs, \
    getResponseHeaders, check_type, add_cookie, \
    get_domain_from_url, divide_list, make_request, gen_poc, get_api
import gevent
from gevent import pool
from socket import error as SocketError

try:
    from bs4 import BeautifulSoup
except ImportError as e:
    LOGGER.warn(e)

static_reg = re.compile(r'\.html$|\.htm$|\.shtml$|\.css$|\.png$|\.js$|\.dpg$|\.jpg$|\.svg$|\.jpeg$|'
                        r'\.gif$|\.webp$|\.ico$|\.woff$|\.ttf$|css\?|js\?|jpg\?|png\?|woff\?v='
                        r'|woff2\?v=|ttf\?|woff\?|woff2$|html\?v=|ico$')
burp_traffic = []
manager = Manager()
case_list = manager.list()
openner_result = manager.list()
traffic_queue = manager.Queue()
traffic_list = manager.list()
reflect_list = manager.list()
api_list = manager.list()

class Traffic_generator(Process):
    DEFAULT_HEADER = {
        'User-Agent': 'Mozilla/2.0 (X11; Linux x86_64) AppleWebKit/237.36 (KHTML, like Gecko) Chrome/62.0.3322.146 Safari/237.36',
    }

    def __init__(self, id, url_list, coroutine):
        Process.__init__(self)
        self.id = id
        self.url_list = url_list
        self.coroutine = coroutine

    def gen_traffic(self, url):
        domain = get_domain_from_url(url)
        cookie = get_cookie(domain)
        self.DEFAULT_HEADER['Cookie'] = cookie
        self.DEFAULT_HEADER['Referer'] = 'https://' + domain + '/'
        request = HttpRequest(method='GET', url=url, headers=self.DEFAULT_HEADER, body='')
        req = urllib.Request(url=url, headers=self.DEFAULT_HEADER)
        with gevent.Timeout(10, False):
            try:
                resp = urllib.urlopen(req)
            except urllib.URLError as e:
                REQUEST_ERROR.append(('gen_traffic()', url, e.reason))
            except CertificateError:
                REQUEST_ERROR.append(('gen_traffic()', url, 'ssl.CertificateError'))
            except (ValueError, BadStatusLine, SocketError, InvalidURL) as e:
                LOGGER.warn(e)
            else:
                if resp.url != url:
                    REDIRECT.append(url)
                try:
                    data = resp.read()
                except Exception as e:
                    LOGGER.warn(e)
                else:
                    resp_headers = resp.headers.items()
                    resp_headers_dict = dict(resp_headers)
                    response = HttpResponse(code=str(resp.code), reason=resp.msg, headers=resp_headers_dict, data=data)
                    return (request, response)

    def run(self):
        import gevent
        from gevent import monkey
        monkey.patch_all()
        g_pool = pool.Pool(self.coroutine)
        tasks = [g_pool.spawn(self.gen_traffic, url) for url in self.url_list]
        gevent.joinall(tasks)
        traffic_list = [i.value for i in tasks if i.value is not None]
        Engine.save_traffic(traffic_list, self.id)

class Detector:
    @staticmethod
    def detect_json(json_str):
        result_dict = {}
        json_str = json_str.replace('\'', '\"')
        try:
            json_dict = json.loads(json_str)
        except ValueError:
            LOGGER.warn('Error in detect_json():%s' % json_str)
        else:
            for k, v in json_dict.items():
                if isinstance(v, str):
                    result_dict[k] = v
                elif isinstance(v, int):
                    result_dict[k] = str(v)
            return result_dict

    @staticmethod
    def parse_by_token(data):
        result = {}
        split_symbol = ','
        data = re.sub(r'[\\\'\"{}\[\]]', '', data)
        if ',' in data:
            groups = data.split(split_symbol)
            for i in groups:
                if ':' in i:
                    k, v = i.split(':')[0], i.split(':')[1]
                    result[k] = v
            return result
        else:
            LOGGER.info('Can\'t parse body:\n%s' % data)

    @staticmethod
    def detect_param(request):
        param_dict = {}
        method, url, body = request.method, request.url, request.body
        if method == 'GET':
            url_parsed = urlparse.urlparse(url)
            param_dict = dict([(k, v[0]) for k, v in urlparse.parse_qs(url_parsed.query).items()])
        elif method == 'POST':
            if body == '':
                return param_dict
            if re.search(r'^{.*}$', body):
                param_dict = Detector.detect_json(body)
            elif re.search(r'^.*?={.*?}$', body):
                body = re.search(r'^.*?=({.*?})$', body).group(1)
                param_dict = Detector.detect_json(body)
            elif request.get_header('Content-Type') and 'multipart/form-data; boundary=' in request.get_header(
                    'Content-Type'):
                pass
            elif '&' not in body:
                param_dict = Detector.parse_by_token(body)
                if param_dict:
                    return param_dict
            else:
                if '&' in body:
                    tmp = body.split('&')
                    for i in tmp:
                        try:
                            param, value = i.split('=')[0], i.split('=')[1]
                        except IndexError:
                            pass
                        else:
                            if param not in param_dict:
                                param_dict[param] = value
                else:
                    tmp = body.split('=')
                    param_dict[tmp[0]] = tmp[1]
        return param_dict

    @staticmethod
    def make_reg(value):
        js_reg = re.compile('<script.*?>.*?' + re.escape(value) + '.*?</script>', re.S)
        html_reg = re.compile('<.*?>.*?' + re.escape(value) + '.*?</[a-zA-Z]{1,10}?>', re.S)
        tag_reg = re.compile('=\"' + re.escape(value) + '\"|=\'' + re.escape(value) + '\'', re.M)
        func_reg = re.compile('\\(.*?' + re.escape(value) + '.*?\\)')
        reg_list = [js_reg, html_reg, tag_reg, func_reg]
        return reg_list

    @staticmethod
    def detect_position(response, value):
        if len(value) <= 1:
            return
        position = []
        response_data = response.data
        response_code = response.code
        reg_list = Detector.make_reg(value)
        js_reg, html_reg, tag_reg, func_reg = reg_list
        if not response_code.startswith('3'):
            if isinstance(response_data, str):
                response_data = response_data.encode('utf-8')
            if value in response_data:
                content_type = response.get_header('Content-Type')
                if content_type:
                    if 'text/html' in content_type:
                        if not re.search('<html.*?</html>', response_data, re.S):
                            position.append('html')
                        if re.search(js_reg, response_data):
                            type = check_type(value)
                            bs = BeautifulSoup(response_data, 'lxml')
                            script_tag_list = bs.find_all('script')
                            if type == 'string':
                                for i in script_tag_list:
                                    js_code = i.text.encode('utf-8')
                                    js_code = js_code.replace(' ', '')
                                    if value in js_code:
                                        if re.search('\'[^\"\']*?' + re.escape(value) + '[^\"\']*?\'', js_code, re.I):
                                            position.append('jssq')
                                        else:
                                            position.append('jsdq')
                            else:
                                for i in script_tag_list:
                                    js_code = i.text.encode('utf-8')
                                    js_code = js_code.replace(' ', '')
                                    if value in js_code:
                                        if re.search('\'[^\"]*?' + re.escape(value) + '[^\"]*?\'', js_code, re.I):
                                            position.append('jssq')
                                        if re.search('[+\\-*/%=]{value}[^\"\']*?;|[+\\-*/%=]{value}[^\"\']*?;'.format(
                                                value=re.escape(value)), js_code):
                                            jsnq_match = re.search('[+\\-*/%=]{value}[^\"\']*?;|[+\\-*/%=]{value}[^\"\']*?;'.format(
                                                value=re.escape(value)), js_code).group()
                                            if re.search(':\"[^\"]*?{value}[^\"]*?\"'.format(value=re.escape(value)), js_code):
                                                pass
                                            elif re.search(r'&amp;$|\\x26amp;$', jsnq_match):
                                                pass
                                            else:
                                                position.append('jsnq')
                                        if re.search('\"[^\'\"]*?' + re.escape(value) + '[^\'\"]*?\"', js_code, re.I):
                                            position.append('jsdq')
                        if re.search(html_reg, response_data):
                            position.append('html')
                        if re.search(tag_reg, response_data):
                            position.append('tag')
                        func_match = re.search(func_reg, response_data)
                        if func_match:
                            result = func_match.group()
                            if len(result) < 50:
                                position.append('func')
                else:
                    position.append('html')
        if 'jsdq' in position or 'jssq' in position or 'jsnq' in position:
            position.append('js')
        return position


class Processor:
    def __init__(self, traffic_obj):
        self.request, self.response = traffic_obj[0], traffic_obj[1]
        self.param_dict = {}
        self.reflect = []

    def process_param(self):
        rtn = Detector.detect_param(self.request)
        if rtn:
            self.param_dict = rtn

    @functimeout(30)
    def process_reflect(self):
        for param, value in self.param_dict.items():
            if len(value) > 1:
                position = Detector.detect_position(self.response, value)
                if position:
                    self.reflect.append((param, value, position))
                    if 'jssq' in position or 'jsnq' in position or 'tag' in position or 'func' in position:
                        reflect_list.append((self.request.url, param, value, position))

    def process_page(self):
        content_type = self.response.get_header('Content-Type')
        if content_type and 'text/html' in content_type and self.response.data:
            self.ispage = True
        else:
            self.ispage = True

    @staticmethod
    def get_process_chains():
        return set(list(filter(lambda m: not m.startswith("__") and not m.endswith("__") and callable(getattr(Processor, m)),
                               dir(Processor)))) - {'run', 'get_process_chains', }

    def run(self):
        for i in Processor.get_process_chains():
            func = getattr(self, i)
            try:
                func()
            except Func_timeout_error as e:
                LOGGER.warn(str(e) + self.request.url)


class Scan(Process):
    PAYLOADS = (
        ('html', '<xsshtml></xsshtml>', '<xsshtml></xsshtml>'),
        ('jsdq', 'xssjs";', '<script.*?xssjs";.*?</script>'),
        ('jssq', 'xssjs\';', '<script.*?xssjs\';.*?</script>'),
        ('jsnq', 'xssjs;', '<script.*?xssjs;.*?</script>'),
        ('tag', 'xsstag"', '="xsstag""|="xsstag" "'),
        ('js', 'xss</script>', '<script.*?xss</script>'),
    )

    def __init__(self):
        Process.__init__(self)

    def rfxss(self, processor):
        rfxss_case_list = []
        if processor.reflect:
            request = processor.request
            method, url, headers, body = request.method, request.url, request.headers, request.body
            reflect = processor.reflect
            if method == 'GET':
                for i in reflect:
                    param, value, position = i[0], i[1], i[2]
                    for location, payload, match in self.PAYLOADS:
                        if location in position:
                            new_url = change_by_param(url, param, payload)
                            case = Case(vul='Reflected XSS', method='GET', url=new_url, headers=headers, body='',
                                        args=(location, match, param, value))
                            rfxss_case_list.append(case)
                return rfxss_case_list
            elif method == 'POST':
                for i in reflect:
                    param, value, position = i[0], i[1], i[2]
                    for location, payload, match in self.PAYLOADS:
                        if location in position:
                            new_body = body.replace(value, payload)
                            case = Case(vul='Reflected XSS', method='POST', url=url, headers=headers, body=new_body,
                                        args=(location, match, param, value))
                            rfxss_case_list.append(case)
                return rfxss_case_list

    def run(self):
        while True:
            try:
                traffic_obj = traffic_queue.get(timeout=3)
            except Empty:
                LOGGER.warn('traffic_queue is empty!')
                time.sleep(1)
            else:
                if traffic_obj is None:
                    break
                else:
                    processor = Processor(traffic_obj)
                    processor.run()
                    if processor.reflect:
                        rtn = self.rfxss(processor)
                        if rtn and isinstance(rtn, list):
                            case_list.extend(rtn)


class Verify:
    ERROR_COUNT = 0

    @staticmethod
    def verify(response, args):
        match = args[1]
        location = args[0]
        if isinstance(response, str):
            content = response
            if location == 'html' and re.search(match, content):
                bs = BeautifulSoup(content, 'lxml')
                xsshtml_tag_list = bs.find_all('xsshtml')
                if xsshtml_tag_list:
                    return True
            elif re.search(match, content, re.S):
                return True
        else:
            content = response.read()
            if location == 'html' and re.search(match, content):
                bs = BeautifulSoup(content, 'lxml')
                xsshtml_tag_list = bs.find_all('xsshtml')
                if xsshtml_tag_list:
                    return True
            elif re.search(match, content, re.S):
                return True

    @staticmethod
    def request_and_verify(case):
        vul, method, url, headers, body, args = case.vul, case.method, case.url, case.headers, case.body, case.args
        old_param, old_value = args[2], args[3]
        LOGGER.info('Verify: %s' % url)
        with gevent.Timeout(20, False):
            resp = make_request(method, url, headers, body)
            if resp:
                if Verify.verify(resp, args):
                    poc = gen_poc(method, url, body, old_param, old_value)
                    LOGGER.critical('Found cross-site script vulnerability(%s) in %s' % (vul, poc))
                    result = (vul, url, poc)
                    return result
            else:
                Verify.ERROR_COUNT += 1

    @staticmethod
    def verify_async(case_list, coroutine):
        from gevent import monkey
        monkey.patch_all()
        result = []
        geventPool = pool.Pool(coroutine)
        tasks = [geventPool.spawn(Verify.request_and_verify, case) for case in case_list]
        gevent.joinall(tasks)
        result.extend([i.value for i in tasks if i.value is not None])
        LOGGER.info('Total %s verify cases, %s error happened.' % (len(case_list), Verify.ERROR_COUNT))
        return result

    class Openner(Process):
        def __init__(self, browser_type, case_list):
            Process.__init__(self)
            self.browser = browser_type
            self.case_list = case_list

        def reload(self, browser):
            browser.quit()
            if self.browser == 'chrome':
                browser = chrome()
            elif self.browser == 'chrome-headless':
                browser = chrome(headless=True)
            else:
                browser = phantomjs()
            add_cookie(browser, self.case_list[0].url)
            return browser

        def handle_block(self, browser):
            try:
                browser.execute_script('window.open();')
                handlers = browser.window_handles
                browser.switch_to_window(handlers[-1])
            except Exception:
                browser = self.reload(browser)
                return browser

        def run(self):
            blocked_urls = []
            if self.browser == 'chrome':
                browser = chrome()
            elif self.browser == 'chrome-headless':
                browser = chrome(headless=True)
            else:
                browser = phantomjs()
            add_cookie(browser, self.case_list[0].url)
            for case in self.case_list:
                if case.method == 'POST':
                    continue
                vul, url, args = case.vul, case.url, case.args
                path = '/'.join(url.split('/', 3)[:3])
                if path not in blocked_urls:
                    try:
                        browser.get(url)
                    except TimeoutException as e:
                        LOGGER.warn(e)
                        REQUEST_ERROR.append(('Openner get()', url, 'timeout'))
                        rtn = self.handle_block(browser)
                        if rtn is not None:
                            browser = rtn
                            blocked_urls.append(path)
                    except BadStatusLine as e:
                        LOGGER.warn(e)
                        REQUEST_ERROR.append(('Render get()', url, 'BadStatusLine'))
                        blocked_urls.append(path)
                    except UnicodeDecodeError:
                        pass
                    else:
                        try:
                            page_source = browser.page_source
                        except UnexpectedAlertPresentException:
                            alert = browser.switch_to.alert
                            alert.accept()
                            page_source = browser.page_source
                        if Verify.verify(page_source, args):
                            poc = gen_poc('GET', url, '')
                            result = (vul, url, poc)
                            openner_result.append(result)
            browser.quit()

    @staticmethod
    def verify_with_browser(browser_type, case_list, process_num):
        open_task = []
        i = len(case_list)
        k = 0
        if i > process_num:
            j = i // process_num
            for i in range(process_num):
                if i == process_num - 1:
                    cases = case_list[k:]
                else:
                    cases = case_list[k:j * (i + 1)]
                    k = j * (i + 1)
                t = Verify.Openner(browser_type, cases)
                open_task.append(t)
        else:
            cases = case_list
            t = Verify.Openner(browser_type, cases)
            open_task.append(t)
        for i in open_task:
            i.start()
        for i in open_task:
            i.join()


class Render(Process):
    def __init__(self, id, browser, url_list):
        Process.__init__(self)
        self.id = id
        self.url_list = url_list
        self.browser = browser

    def reload(self, browser):
        browser.quit()
        if self.browser == 'chrome':
            browser = chrome()
        elif self.browser == 'chrome-headless':
            browser = chrome(headless=True)
        else:
            browser = phantomjs()
        add_cookie(browser, self.url_list[0])
        return browser

    def handle_block(self, browser):
        try:
            browser.execute_script('window.open();')
            handlers = browser.window_handles
            browser.switch_to.window(handlers[-1])
        except Exception:
            browser = self.reload(browser)
            return browser

    def gen_traffic(self, url, page_source, response_headers):
        request = HttpRequest(method='GET', url=url, headers=Traffic_generator.DEFAULT_HEADER, body='')
        if not response_headers:
            response_headers = {'Content-Type': 'text/html'}
        response = HttpResponse(code='200', reason='OK', headers=response_headers, data=page_source)
        return (request, response)

    def run(self):
        blocked_urls = []
        if self.browser == 'chrome':
            browser = chrome()
        elif self.browser == 'chrome-headless':
            browser = chrome(headless=True)
        else:
            browser = phantomjs()
        add_cookie(browser, self.url_list[0])
        for url in self.url_list:
            path = '/'.join(url.split('/', 3)[:3])
            if path not in blocked_urls:
                try:
                    browser.get(url)
                except TimeoutException as e:
                    LOGGER.warn(e)
                    REQUEST_ERROR.append(('Render get()', url, 'timeout'))
                    rtn = self.handle_block(browser)
                    if rtn is not None:
                        browser = rtn
                        blocked_urls.append(path)
                except BadStatusLine as e:
                    LOGGER.warn(e)
                    REQUEST_ERROR.append(('Render get()', url, 'BadStatusLine'))
                    blocked_urls.append(path)
                except UnicodeDecodeError:
                    pass
                else:
                    try:
                        page_source = browser.page_source
                    except UnexpectedAlertPresentException:
                        alert = browser.switch_to.alert
                        alert.accept()
                        page_source = browser.page_source
                    response_headers = getResponseHeaders(self.browser, browser)
                    traffic = self.gen_traffic(url, page_source, response_headers)
                    if traffic:
                        traffic_list.append(traffic)
        browser.quit()


def url_filter(url):
    if '?' not in url:
        return False
    if static_reg.search(url):
        return False
    else:
        api = get_api(url)
        if api in api_list:
            return False
        else:
            api_list.append(api)
            return url


class Engine(object):
    def __init__(self, id, url, file, burp, process, coroutine, browser, filter):
        self.id = id
        self.url = url
        self.file = file
        self.burp = burp
        self.process = process
        self.coroutine = coroutine
        self.browser = browser
        self.filter = filter

    def put_queue(self):
        traffic_path = []
        files = os.listdir(TRAFFIC_DIR)
        for i in files:
            if re.search(self.id + '.traffic\d*', i):
                traffic_path.append(os.path.join(TRAFFIC_DIR, i))
        for i in traffic_path:
            with open(i, 'rb') as f:
                traffic_list = pickle.load(f)
                LOGGER.info('Start to put traffic(used %s) into traffic_queue, total is %s.' % (i, len(traffic_list)))
                for traffic in traffic_list:
                    traffic_queue.put(traffic)

    def send_end_sig(self):
        for i in range(self.process):
            traffic_queue.put(None)

    def put_burp_to_trafficqueue(self):
        if os.path.exists(self.burp):
            import base64
            from xml.etree import cElementTree as ET
            from model import HttpRequest, HttpResponse
            with open(self.burp) as f:
                xmlstr = f.read()
            try:
                root = ET.fromstring(xmlstr)
            except cElementTree.ParseError as e:
                LOGGER.error('Parse burpsuite data error: ' + str(e))
                exit(0)
            for child in root:
                if child.tag == 'item':
                    req_headers = {}
                    resp_headers = {}
                    code = ''
                    request, response = '', ''
                    for child2 in child:
                        if child2.tag == 'method':
                            method = child2.text
                        if child2.tag == 'url':
                            url = child2.text
                            if static_reg.search(url):
                                break
                        if child2.tag == 'status':
                            code = child2.text
                        if child2.tag == 'request':
                            req_text = child2.text
                            req_text = base64.b64decode(req_text).decode('utf-8')
                            headers_list = req_text.split('\r\n\r\n', 1)[0].split('\r\n')[1:]
                            for header in headers_list:
                                try:
                                    header_key, header_value = header.split(': ')[0], header.split(': ')[1]
                                    if header_key not in req_headers.keys():
                                        req_headers[header_key] = header_value
                                except IndexError as e:
                                    LOGGER.warn(e)
                            body = req_text.split('\r\n\r\n', 1)[1]
                            request = HttpRequest(method, url, req_headers, body)
                        if child2.tag == 'response':
                            resp_text = child2.text
                            if resp_text:
                                resp_text = base64.b64decode(resp_text).decode('utf-8')
                                reason = resp_text.split('\r\n')[0]
                                headers_list = resp_text.split('\r\n\r\n', 1)[0].split('\r\n')[1:]
                                for header in headers_list:
                                    header_key, header_value = header.split(': ')[0], header.split(': ')[1]
                                    if header_key not in resp_headers.keys():
                                        resp_headers[header_key] = header_value
                                data = resp_text.split('\r\n\r\n', 1)[1]
                                response = HttpResponse(code, reason, resp_headers, data)
                    if request and response:
                        if request.method == 'GET' and '?' in request.url:
                            if not static_reg.search(url):
                                burp_traffic.append((request, response))
                                traffic_queue.put((request, response))
                        elif request.method == 'POST' and request.body:
                            content_type = request.get_header('Content-Type')
                            if content_type and 'multipart/form-data; boundary=' in content_type:
                                MULTIPART.append((request, response))
                            else:
                                burp_traffic.append((request, response))
                                traffic_queue.put((request, response))
        else:
            LOGGER.error('%s not exists!' % self.burp)

    @staticmethod
    def get_traffic_path(id):
        return os.path.join(TRAFFIC_DIR, id + '.traffic')

    def get_render_task(self, url_list):
        render_task = []
        i = len(url_list)
        k = 0
        if i > self.process:
            j = i // self.process
            for i in range(self.process):
                if i == self.process - 1:
                    urls = url_list[k:]
                else:
                    urls = url_list[k:j * (i + 1)]
                    k = j * (i + 1)
                t = Render(self.id, self.browser, urls)
                render_task.append(t)
        else:
            urls = url_list
            t = Render(self.id, self.browser, urls)
            render_task.append(t)
        return render_task

    def deduplicate(self, url_list):
        LOGGER.info('Start to deduplicate for all urls.')
        filtered_path = self.file + '.filtered'
        if os.path.exists(filtered_path):
            LOGGER.info('%s has been filtered as %s.' % (self.file, filtered_path))
            with open(filtered_path) as f:
                filtered = f.read().split('\n')
                return filtered
        filtered = []
        from multiprocessing import cpu_count
        from multiprocessing.pool import Pool
        p = Pool(cpu_count())
        result = p.map(url_filter, url_list)
        for i in result:
            if isinstance(i, str):
                filtered.append(i)
        with open(filtered_path, 'w') as f:
            f.write('\n'.join(filtered))
        LOGGER.info('Saved filtered urls to %s.' % filtered_path)
        return filtered

    def save_reflect(self):
        if reflect_list:
            reflect_path = self.get_traffic_path(self.id).replace('.traffic', '.reflect')
            with open(reflect_path, 'wb') as f:
                pickle.dump(list(reflect_list), f)

    @staticmethod
    def save_traffic(traffic_obj_list, id, piece=3000):
        traffic_path = Engine.get_traffic_path(id)
        if traffic_obj_list:
            saved_traffic_list = list(traffic_obj_list)
            if len(saved_traffic_list) > piece:
                traffic_divided_path = []
                traffic_divided = divide_list(saved_traffic_list, piece)
                for i, traffic in enumerate(traffic_divided):
                    path = traffic_path + str(i)
                    traffic_divided_path.append(path)
                    with open(path, 'wb') as traffic_f:
                        pickle.dump(traffic, traffic_f)
                LOGGER.info('Traffic of %s has been divided and saved to %s.' % (id, ','.join(traffic_divided_path)))
            else:
                with open(traffic_path, 'wb') as traffic_f:
                    pickle.dump(saved_traffic_list, traffic_f)
                LOGGER.info('Traffic of %s has been saved to %s.' % (id, traffic_path))

    def save_request_exception(self):
        if REQUEST_ERROR:
            with open(self.get_traffic_path(self.id).replace('.traffic', '.error'), 'wb') as f:
                pickle.dump(REQUEST_ERROR, f)

    def save_redirect(self):
        if REDIRECT:
            with open(self.get_traffic_path(self.id).replace('.traffic', '.redirect'), 'wb') as f:
                pickle.dump(REDIRECT, f)

    def save_multipart(self):
        if MULTIPART:
            with open(self.get_traffic_path(self.id).replace('.traffic', '.multipart'), 'wb') as f:
                pickle.dump(MULTIPART, f)

    def save_analysis(self):
        LOGGER.info('Total multipart is: %s, redirect is: %s, request exception is: %s' % (
            len(MULTIPART), len(REDIRECT), len(REQUEST_ERROR)))
        self.save_multipart()
        self.save_redirect()
        self.save_request_exception()

    def urldecode(self, url_list):
        for i in range(len(url_list)):
            if '%' in url_list[i]:
                url_list[i] = urllib2.unquote(url_list[i])
        return url_list

    @staticmethod
    def is_scanned(id):
        files = os.listdir(TRAFFIC_DIR)
        for i in files:
            if re.search(id + '\.traffic\d*', i):
                return True

    def start(self):
        if self.is_scanned(self.id):
            choice = input('Task %s has been scanned, do you want to rescan?(Y/N)' % self.id)
            if choice.lower() in ['y', 'yes']:
                self.put_queue()
                self.send_end_sig()
            elif choice.lower() in ['n', 'no']:
                exit(0)
            else:
                LOGGER.error('Incorrect choice.')
                exit(0)
        elif self.burp:
            self.put_burp_to_trafficqueue()
            self.send_end_sig()
            if burp_traffic:
                self.save_traffic(burp_traffic, self.id)
        else:
            if self.url:
                url_list = [self.url]
            elif self.file:
                if os.path.exists(self.file):
                    with open(self.file) as f:
                        url_list = [url.strip() for url in f.read().split('\n') if url.strip()]
                        if not self.file.endswith('.slice'):
                            url_list = self.deduplicate(url_list)
                        if self.filter:
                            exit(0)
                else:
                    LOGGER.error('%s not exists!' % self.file)
                    exit(0)
            url_list = self.urldecode(url_list)
            if self.browser:
                LOGGER.info('Start to request url with %s.' % self.browser)
                render_task = self.get_render_task(url_list)
                for i in render_task:
                    i.start()
                for i in render_task:
                    i.join()
                self.save_traffic(traffic_list, self.id)
                for i in range(len(traffic_list)):
                    request = traffic_list[i][0]
                    response = traffic_list[i][1]
                    traffic_queue.put((request, response))
                self.send_end_sig()
            else:
                LOGGER.info('Start to request url with urllib2.')
                traffic_maker = Traffic_generator(self.id, url_list, self.coroutine)
                traffic_maker.start()
                traffic_maker.join()
                self.put_queue()
                self.send_end_sig()
        task = [Scan() for _ in range(self.process)]
        for i in task:
            i.start()
        for i in task:
            i.join()
        self.save_reflect()
        if case_list:
            if self.browser:
                Verify.verify_with_browser(self.browser, case_list, self.process)
                self.save_analysis()
                return openner_result
            else:
                verify_result = Verify.verify_async(case_list, self.coroutine)
                self.save_analysis()
                return verify_result

if __name__ == '__main__':
    pass