"""
# 远程采集发布助手：带采集和FTP上传功能
# 格式化导包顺序： pip install isort,然后：isort main.py
# 单独接口文档地址： https://gitee.com/lopins/notes
# 重置采集日志
# UPDATE db_39cbb3 SET title = NULL, tags = NULL, publish = NULL;
# UPDATE db_d611b7 SET title = NULL, tags = NULL, publish = NULL;
"""
# from base64 import b64encode, b64decode
# 引入 collections.abc 中的 Iterable
import collections.abc
from base64 import b64decode, b64encode
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

collections.Iterable = collections.abc.Iterable
import json
import logging
import mimetypes
import os
import random
import re
import sqlite3
import threading
import time
from ftplib import FTP, error_perm
from hashlib import md5
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, urlsplit

import jieba
import jieba.analyse
import mysql.connector
import requests
from bs4 import BeautifulSoup
from bs4.element import ResultSet, Tag
from html2text import HTML2Text
from markdown import markdown
from PIL import Image, ImageDraw, ImageFont
from pypinyin import Style, pinyin
from text_blind_watermark import TextBlindWatermark, TextBlindWatermarkThin
from translate import Translator
from urllib3 import PoolManager, disable_warnings, exceptions

# 禁用警告
disable_warnings(exceptions.InsecureRequestWarning)
# html2text转换设置,config.py
h = HTML2Text(bodywidth=0)
h.ignore_links = True  # 忽略链接

# 禁用结巴分词调试输出
jieba.setLogLevel(logging.INFO)
# 配置日志记录
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
log_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - Line: %(lineno)d - %(message)s')
# 配置控制台日志处理程序
ch = logging.StreamHandler()
ch.setFormatter(log_format)
logger.addHandler(ch)
# 配置文件日志处理程序
file_handler = logging.FileHandler('spider.log')
file_handler.setFormatter(log_format)
logger.addHandler(file_handler)

"""文章采集类"""
class ArticleCollector(object):
    def __init__(self, site_args: dict):
        # 源头网站参数导入
        self.base_url = site_args["base_url"]
        self.cat_list = site_args["cat_list"]
        self.web_selector = site_args["web_selectors"]
        self.watermark = site_args["watermark"]
        self.filter_title = site_args["filter_titles"]
        self.remove_selectors = site_args["remove_selectors"]
        self.bad_words = site_args["bad_words"]
        self.segk_list = site_args["segk_list"]
        # 目标网站参数导入
        self.web_configs = site_args["web_configs"]
        # 脚本运行模式设置
        self.run_mode = site_args["run_mode"]
        # 脚本运行模式设置
        self.db_config = site_args["db_config"]
        # 当前脚本所在的目录
        self.script_dir = Path(__file__).resolve().parent
        self.cur_dir = self.script_dir / urlsplit(self.base_url).hostname   # 任务目录
        if self.run_mode["database"] == 'mysql':
            self.tname = f"db_{md5(urlsplit(self.base_url).hostname.encode()).hexdigest()[13:19]}"
            self.database = ArticleDataBase('mysql', self.db_config)
            try:
                tables = self.database.select_data(
                    "`information_schema`.`tables`",
                    "table_name",
                    f"table_name = '{self.tname}'"
                )
                if (self.tname,) not in tables:
                    c_t = f"""
                        `id` INT AUTO_INCREMENT PRIMARY KEY,
                        `source` VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL UNIQUE,
                        `title` VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
                        `tags` JSON DEFAULT NULL,
                        `publish` TINYINT(1) DEFAULT NULL
                    """
                    self.database.create_table(self.tname, c_t)
                    logger.info(f'新建数据库成功，{self.tname}')
                    """查询重复项： SELECT source, COUNT(*) AS count FROM db_39cbb3 GROUP BY source HAVING count > 1;"""
            except Exception as e:
                logger.error(f'数据库操作失败，{str(e)}')
        elif self.run_mode["database"] == 'sqlite':
            # 当前表名
            self.tname = f"db_{md5(urlsplit(self.base_url).hostname.encode()).hexdigest()[13:19]}"
            self.database = ArticleDataBase('sqlite', self.db_config)
            # 执行查询以获取所有表名
            # f_a = f"SELECT `name` FROM `sqlite_master` WHERE `type` = 'table' ORDER BY `name`;"
            # tables = self.database.fetch_all(f_a)
            tables = self.database.select_data("sqlite_master","name", "type='table'")
            # 检查表是否存在
            if (self.tname,) not in tables:
                # 定义创建表的SQL语句
                c_t = f"""id INTEGER PRIMARY KEY, source TEXT, title TEXT, tags TEXT, publish INTEGER"""
                self.database.create_table(self.tname, c_t)
            else:
                print(f"数据表已存在：{self.tname}")
        else:
            self.logs_dir = self.script_dir / 'logs'
            os.makedirs(self.logs_dir, exist_ok=True)
            # 需采集内容所有链接
            self.urls_list = os.path.join(self.logs_dir, f'{urlsplit(self.base_url).hostname}_source.link')  # 所有链接
            # 发布成功的内容链接
            self.publish = os.path.join(self.logs_dir, f'{urlsplit(self.base_url).hostname}_complete.txt')  # 已发布完
        # 网络请求（含重试请求）参数
        self.headers = {
            'User-Agent': "Mozilla/5.0 (compatible; Baiduspider; +http://www.baidu.com/search/spider.html)",
            'Referer': self.base_url,
        }
        self.s, self.RETRY_TIMES, self.RETRY_DELAY = requests.session(), 5, 3

    # 获取需要的链接
    def get_post_list(self):
        # 定义变量用于存储待爬取链接、已爬取的链接
        all_links, exist_links = set(), set()

        # 抓取单个列表上符合条件的链接
        def process_cat(url):
            for i in range(self.RETRY_TIMES):
                try:
                    _ = self.get_with_retry(url)
                    _soup = BeautifulSoup(_.text, "lxml")
                    p_l = {
                        (self.base_url + _["href"]) if "http" not in _["href"] else _["href"]
                        for _ in _soup.select(self.web_selector["list"]["link"])
                    }
                    if self.run_mode["database"] == "txt":
                        with open(self.urls_list, 'a+', encoding='utf-8') as f_l:
                            with list_lock:
                                _pl = []
                                for pl in p_l:
                                    if pl not in exist_links:
                                        f_l.write(f"{pl}\n")
                                        _pl.extend(pl)
                                logger.info(f'写入：{url},共有链接，{len(_pl)}')
                                # 低性能写入
                                # [f_l.write(f"{pl}\n") for pl in p_l if pl not in exist_links]
                    else:
                        with list_lock:
                            for pl in p_l:
                                self.database.insert_data(self.tname, {'source': pl})
                                logger.info(f'{pl}写入成功')
                            # 低性能写入
                            # [self.database.insert_data(self.tname, {'source': pl}) for pl in p_l]
                except Exception as e:
                    if i == self.RETRY_TIMES - 1:
                        logger.error(f'请求列表 {url} 失败 {self.RETRY_TIMES} 次，请手动检查')
                    else:
                        time.sleep(self.RETRY_DELAY)

        # 是否重新爬取网站全部链接
        if self.run_mode["database"] == "txt":
            # 获取已经存在的链接
            if not os.path.exists(self.urls_list):
                open(self.urls_list, 'w', encoding='utf-8').close()
            exist_links.update({_.strip() for _ in open(self.urls_list, 'r', encoding='utf-8').readlines()})
            tiaojian = not os.path.exists(self.urls_list) or os.path.getsize(self.urls_list) == 0 or len(
                exist_links) == 0
        else:
            # 获取已经存在的链接
            exist_links.update(set([_[0] for _ in self.database.select_data(self.tname, "source")]))
            tiaojian = self.run_mode["recrawl"] or len(exist_links) == 0

        # 判断是否进行抓取
        if tiaojian:
            list_lock = threading.Lock()
            # 开始抓取所有链接
            with ThreadPoolExecutor(max_workers=self.run_mode["threads"]) as executor:
                executor.map(process_cat, self.cat_list)
            # [process_cat(_) for _ in self.cat_list]
        else:
            logger.info(f'无需抓取，如需抓取，请mode["recrawl"]的值设置为True')

    # 清洗获取的内容
    def handle_post_data(self):
        while True:
            links = []
            # 需要处理的链接
            if self.run_mode["database"] == "txt":
                if not os.path.exists(self.urls_list):
                    open(self.urls_list, 'w', encoding='utf-8').close()
                # 重新从文件中获取一次新的链接数据
                _links = {_.strip() for _ in open(self.urls_list, 'r', encoding='utf-8').readlines()}
                # 获取已经采集完毕的链接
                if not os.path.exists(self.publish):
                    open(self.publish, 'w', encoding='utf-8').close()
                links = [_ for _ in _links - {_.strip() for _ in open(self.publish, 'r', encoding='utf-8').readlines()}]
            else:
                rl = f"""RAND()""" if self.run_mode["database"] == "mysql" else f"""RANDOM()"""
                links = [_[0] for _ in self.database.select_data(
                    self.tname,
                    "source",
                    f"publish IS NULL ORDER BY {rl} LIMIT 1")
                ]

            if not links:
                logger.error(f'{links}，没有链接，跳过')
                continue
            else:
                link = random.choice(links)

            # link = 'https://www.baikew.net/shcs/211267.html'
            # 加载网站信息，准备发布内容
            web_parms = random.choice(self.web_configs)
            res = self.get_with_retry(link)
            if res is None:
                logger.error(f'{link}，抓取失败，跳过')
                continue
            try:
                # 部分有乱码（疑似Emoji）,此处中转编码
                _ = res.content.decode('utf-8', 'ignore').encode('utf-8')
                _soup = BeautifulSoup(_, "lxml")
                """ 获取想要的标签属性 """
                try:
                    _title = _soup.select_one(f'{self.web_selector["content"]["title"]}').text.strip().lstrip("标题：")
                    # 只保留需要的特定标题信息
                    if len(self.filter_title) > 0 and any(_ in _title for _ in self.filter_title):
                        continue
                except:
                    continue

                if web_parms["web_config"]["web_category"] is not None:
                    _cate = random.choice(web_parms["web_config"]["web_category"])
                else:
                    _cate = 1

                # HTML->Markdown->HTML,过滤掉多余的HTML标签
                # 假设文章内容在以下变量中
                try:
                    _html = _soup.select_one(f'{self.web_selector["content"]["content"]}')
                except:
                    continue

                try:
                    _excerpt = _soup.select_one(f'{self.web_selector["content"]["excerpt"]}').text.strip().lstrip("摘要：")
                except:
                    try:
                        # _excerpt = _html.find('p').get_text().strip()
                        # 获取所有的段落标签
                        paragraphs = _html.find_all('p')
                        # 检查第一段是否仅包含图片（没有文本内容）
                        if paragraphs and not paragraphs[0].get_text().strip():
                            if len(paragraphs) > 1:
                                _excerpt = paragraphs[1].get_text().strip()
                            else:
                                _excerpt = ""
                        else:
                            _excerpt = paragraphs[0].get_text().strip()
                    except:
                        _excerpt = ""

                # 移除不需要的元素，BeautifulSoup对象转字符串
                if len(self.remove_selectors["tags"]) > 0:
                    for ele_tag in self.remove_selectors["tags"]:
                        try:
                            [ele.extract() for ele in _html.select(ele_tag)]
                        except:
                            pass

                # BeautifulSoup对象转字符串，使用正则表达式
                if len(self.remove_selectors["regex"]) > 0:
                    for regex_pattern in self.remove_selectors["regex"]:
                        try:
                            _ = ''.join(re.sub(regex_pattern, "", str(_html)))
                            _html = BeautifulSoup(_, "lxml")
                        except:
                            pass

                """ 获取想要的图片文件 """
                _local, _thumb = [], []
                try:
                    # 遍历每个img标签（文章内），处理图片连接，修改其src属性
                    img_tags = _html.find_all("img")
                    # 如果有图片标签并且不是Debug模式，则处理图片链接
                    if len(img_tags) > 0:
                        # 下载和替换图片链接（BeautifulSoup对象）
                        _html, _local, _thumb = self.fix_image_realurl(link, img_tags, _title, _html, _local, _thumb)
                except Exception as e:
                    logger.error(f'{link}，获取图片失败，{str(e)}')
                """ 获取想要的图片文件 """

                # 获取处理后的文章内容文本字符串
                _text = markdown(h.handle(''.join([str(con) for con in _html.contents])))

                # 获取文章标签/关键词，或者基于TF-IDF规则分词
                if self.run_mode["nlpsplit"]:
                    _tags = ArticlePublish().extract_keywords(_text, self.segk_list)
                else:
                    try:
                        _tags = [
                            kw.text.strip() for kw in _soup.select(f'{self.web_selector["content"]["tags"]}')
                            if kw.text.strip()
                        ]
                    except:
                        try:
                            _ = _soup.find('meta', attrs={"name": "keywords"}).get('content', '')
                            _tags = [kw.strip() for kw in _.split(',') if kw.strip()]
                        except:
                            _tags = []
                            # 如果标签过少，或者强制分词，则启用TF-IDF搜索引擎规则分词
                    if any('(' in _ for _ in _tags):
                        _tags = [re.sub(r'[^\w\s]', '', _) for tag in _tags for _ in re.split(r'[()]', tag) if _]
                    if len(_tags) < 1 or any(_.startswith('标签：') for _ in _tags) or any(' ' in _ for _ in _tags):
                        _tags = ArticlePublish().extract_keywords(_text, self.segk_list)
                    _tags = [_ for _ in _tags if re.search(r'[\u4e00-\u9fa5a-zA-Z]', _)]

                """ 获取想要的标签属性 """

                if self.run_mode["forbid"]:
                    # 过滤违禁词信息
                    if len(self.bad_words) > 0 and any(_ in _title for _ in self.bad_words):
                        continue
                    if len(self.bad_words) > 0 and any(_ in _text for _ in self.bad_words):
                        continue

                # 打入文本盲水印password是加密密码，watermark是盲水印文字
                # 一行写法
                # _text = (lambda text: TextBlindWatermark(password=password).read_wm(watermark=watermark).read_text(text=text).embed())(_text)
                # 多行写法
                # twm = TextBlindWatermark(password=password)
                # twm.read_wm(watermark=watermark)
                # twm.read_text(text=_text)
                # _text = twm.embed()

                # 构建字段，涉及emoji需要utf-8编码，_title.encode().decode()
                _info = {
                    'source': link,
                    'title': _title,
                    'content': _text,
                    'category': _cate,
                    'tags': [str(_) for _ in _tags][:5],
                    'excerpt': _excerpt,
                    'thumbnail': _thumb,
                    'thumblocal': _local
                }

                # 判断标题中是否有乱码
                if self.run_mode["garble"]:
                    if re.compile(r'[\u4e00-\u9fa5a-zA-Z]').search(_title+_excerpt+_text) is None:
                        continue

                # 发布信息内容
                if self.run_mode["run"] == "Debug":
                    logger.info(_info)
                else:
                    flag = self.publish_content_data(web_parms, _info)
                    if flag and any(_ in flag["status"] for _ in ['成功', '存在', '重复']):
                        if web_parms["ftp_config"]["ftp_status"] and len(_info["thumblocal"]) > 0:
                            # 开始上传图片文件：先下载到本地，然后从本地上传
                            _ftp = self.ftp_upload_images(web_parms["ftp_config"], _info["thumblocal"])
                            ftp = {'ftp': '上传成功'} if _ftp else {'ftp': '上传失败'}
                            flag = dict(**flag, **ftp)
                    else:
                        pass
                    logger.info(flag)
            except Exception as e:
                logger.error(f'{link}，获取源失败: {str(e)}')

        # 调试模式下，程序结束删除所有链接
        if self.run_mode["run"] == "Debug":
            if self.run_mode["database"] == "txt":
                if os.path.exists(self.urls_list):
                    os.remove(self.urls_list)
                if os.path.exists(self.publish):
                    os.remove(self.publish)
            else:
                # 提示用户确认
                confirmation = input("谨慎操作！！！ 您确认将表中所有数据都设置为NULL? (yes/no): ")
                if confirmation.lower() == "yes":
                    self.database.update_data(self.tname, {'title': None, 'tags': None, 'publish': None})
                    logger.info("操作成功！！！ 所有数据都设置为NULL.")

    # 处理内容图片
    def fix_image_realurl(self, link: str, imgs: ResultSet, title: str, html: BeautifulSoup, local: list, thumb: list):
        # 预先存储需要使用的变量，方便后续使用
        for img_tag in imgs:
            # 有标签但是无实际值
            if not img_tag["src"]:
                continue
            # 如果图片是Base64格式，则跳过
            # if "data:image/" in img_tag['data-original']: continue
            if "data:image/" in img_tag["src"]:
                continue

            # 优先使用data-original属性，否则使用src属性
            if "data-original" in img_tag.attrs:
                img_url = img_tag["data-original"]
            elif "src" in img_tag.attrs:
                img_url = img_tag["src"]
            else:
                continue

            # 简单修复图片链接, 补全相对路径URL
            if img_url.startswith("//"):
                real_imgurl = urlparse(link).scheme + ":" + img_url
            elif img_url.startswith("/"):
                real_imgurl = urlparse(link).scheme + "://" + urlparse(link).netloc + img_url
            elif img_url.startswith("./"):
                real_imgurl = urljoin(link, img_url)
            elif img_url.startswith("../"):
                real_imgurl = urljoin(link, img_url)
            else:
                real_imgurl = urljoin(link, img_url)

            # 判断修复后的链接是否可以正常访问
            _ = self.get_with_retry(real_imgurl)
            if _ is None:
                # 获取一张备用图
                try:
                    if re.compile(r'[\u4e00-\u9fa5]').search(title):
                        rri_kw = title
                    else:
                        rri_kw = Translator(to_lang="zh", from_lang="en").translate(title)
                    rri = f'http://wp.birdpaper.com.cn/intf/search?content={rri_kw}&pageno={random.randint(1, 9)}&count=100'
                    rri_list = self.get_with_retry(rri).json()["data"]["list"]
                except:
                    if re.compile(r'[\u4e00-\u9fa5]').search(title):
                        rri_kw = title
                    else:
                        rri_kw = Translator(to_lang="zh", from_lang="en").translate(title)
                    rri = f'http://wallpaper.apc.360.cn/index.php?c=WallPaper&a=search&kw={rri_kw}&start=0&count=100'
                    rri_list = self.get_with_retry(rri).json()["data"]
                if rri_list and len(rri_list) > 0:
                    real_imgurl = random.choice(rri_list)["url"]
                else:
                    if re.compile(r'[\u4e00-\u9fa5]').search(title):
                        rri_kw = Translator(to_lang="en", from_lang="zh").translate(title)
                    else:
                        rri_kw = title
                    real_imgurl = self.get_with_retry(f'https://source.unsplash.com/random/1280x720/?{rri_kw}')
            # elif _.status_code < 400:
            #     i_px = Image.open(BytesIO(_.content)).format
            #     if i_px is None:
            #         continue
            # else:
            #     pass

            # 修复远程图片调用解析器隐藏的图片真实地址
            jiexis = [
                '/img.php?url=',
                '/image.php?url=',
                '/link.php?url=',
                '/pic.php?url=',
                '/api.php?url=',
                '/img.php?img=',
                '/image.php?img=',
                '/link.php?img=',
                '/pic.php?img=',
                '/api.php?img=',
                '/img.php?image=',
                '/image.php?image=',
                '/link.php?image=',
                '/pic.php?image=',
                '/api.php?image=',
                '/img.php?pic=',
                '/image.php?pic=',
                '/link.php?pic=',
                '/pic.php?pic=',
                '/api.php?pic=',
            ]

            # 最终需要处理的图片文件链接（需要下载）
            real_imgurl = next((real_imgurl.split(_)[1] for _ in jiexis if _ in real_imgurl), real_imgurl)

            # 本地化图片（下载到本地/自己服务器），如果下载失败则跳过
            ignored_images_domains = [
                '.baidu.com',
                '.byteimg.com',
                '.zhimg.com',
                'user-gold-cdn.xitu.io',
                'upload-images.jianshu.io',
                'img-blog.csdnimg.cn',
                'nimg.ws.126.net',
                '.360buyimg.com',
                '.sinaimg.cn',
                'user-images.githubusercontent.com',
                '.qhimg.com',
                '.alicdn.com',
                '.tbcdn.cn',
                '.cn.bing.net',
                '.sogoucdn.com',
                '.360tres.com'
            ]
            if any(_ in real_imgurl for _ in ignored_images_domains):
                continue
            # 图片重命名
            filename = f"{md5(real_imgurl.encode()).hexdigest()}.jpg"

            # 开始下载图片文件
            down_flag, local_path = self.download_image_with_retry(real_imgurl, filename)
            if down_flag is not None:
                local.append(local_path)

            # 替换成本地化链接，前缀也可以改成绝对链接
            new_imgurl = f"{self.run_mode['imgprefix']}/{filename}"
            if "data-original" in img_tag.attrs:
                img_tag["data-original"] = new_imgurl
            elif "src" in img_tag.attrs:
                img_tag["src"] = new_imgurl
            else:
                continue

            # 图片添加ALT和TITlE属性
            img_tag["alt"] = img_tag["title"] = title

            # BeautifulSoup -> String -> BeautifulSoup
            _text = markdown(h.handle(''.join([str(_) for _ in html.contents])))
            html = BeautifulSoup(_text.replace(img_url, new_imgurl), "lxml")

            # 获取文章的缩略图链接列表
            thumb = list(set(_.get("src") for _ in html.find_all("img") if _.get("src") is not None)) or []

        # 重新返回处理后字符串对象（替换后图片链接内容）
        return html, local, thumb

    # 爬取图片内容，下载图片到本地
    def download_image_with_retry(self, real_imgurl: str, filename: str):
        for i in range(self.RETRY_TIMES):
            try:
                ri = self.get_with_retry(real_imgurl)
                image_path = os.path.join(self.cur_dir, "uploads", filename)
                os.makedirs(os.path.dirname(image_path), exist_ok=True)
                if ri is not None and ri.status_code == 200:
                    with open(image_path, 'wb') as f:
                        f.write(ri.content)
                    if os.path.exists(image_path):
                        return True, image_path
                elif ri is not None and ri.status_code == 301:
                    redirection_url = ri.headers["Location"]
                    ri = self.get_with_retry(redirection_url)
                    if ri is not None and ri.status_code == 200:
                        with open(image_path, 'wb') as f:
                            f.write(ri.content)
                        if os.path.exists(image_path):
                            if self.watermark is not None:
                                self.remove_watermark(image_path)
                            return True, image_path
                else:
                    ri = self.get_with_retry(real_imgurl.replace('https://', 'http://'))
                    if ri is not None and ri.status_code == 200:
                        with open(image_path, 'wb') as f:
                            f.write(ri.content)
                        if os.path.exists(image_path):
                            if self.watermark is not None:
                                self.remove_watermark(image_path)
                            return True, image_path
                    else:
                        return False, real_imgurl
            except:
                if i == self.RETRY_TIMES - 1:
                    return False, real_imgurl
                else:
                    time.sleep(self.RETRY_DELAY)

        return False, real_imgurl

    # 去除文字型水印
    def remove_watermark(self, image_path: str):
        image = Image.open(image_path)
        draw = ImageDraw.Draw(image)
        width, height = image.size
        # font = ImageFont.truetype("arial.ttf", 36)
        # 使用默认字体
        font = None
        text_width, text_height = draw.textsize(self.watermark, font)
        x = width - text_width - 10
        y = height - text_height - 10
        draw.text((x, y), self.watermark, font=font, fill="white")
        image.save(image_path)

    # 失败后重试处理
    def get_with_retry(self, url: str):
        for i in range(self.RETRY_TIMES):
            try:
                rp = self.s.get(url, headers=self.headers, timeout=5)
                if rp.status_code == 200:
                    return rp
                elif rp.status_code == 301 or rp.status_code == 302:
                    rp = self.s.get(rp.headers["Location"], headers=self.headers, timeout=5)
                    if rp.status_code == 200:
                        return rp
            except requests.exceptions.HTTPError as e:
                if self.s.get(url).status_code == 404:
                    logger.error(f'{url}页面不存在: 跳过404错误')
                    return None
                if self.s.get(url).status_code == 500:
                    logger.error(f'{url}网站内部错误: 跳过500错误')
                    return None
                else:
                    logger.error(f'请求错误，状态码：{str(e)}')
                    return None
            except:
                if i == self.RETRY_TIMES - 1:
                    return None
                else:
                    time.sleep(self.RETRY_DELAY)
        return None

    # 上传图片到网站
    def ftp_upload_images(self, ftpconfig: dict, img_paths: list):
        for img_path in img_paths:
            for i in range(self.RETRY_TIMES):
                ftp = None
                try:
                    ftp = FTP()
                    ftp.connect(host=ftpconfig["ftp_server"], port=ftpconfig["ftp_port"])
                    ftp.login(user=ftpconfig["ftp_username"], passwd=ftpconfig["ftp_password"])
                    for cd in list(filter(None, ftpconfig["ftp_uploaddir"].split('/'))):
                        try:
                            ftp.cwd(cd)
                        except error_perm as e:
                            if "550" in str(e):
                                try:
                                    ftp.mkd(cd)
                                    ftp.cwd(cd)
                                except Exception as mkd_err:
                                    logger.error(f"无法创建或进入目录: {str(mkd_err)}")
                            else:
                                logger.error(f"无法进入目录，错误详情: {str(e)}")
                    # 带子目录
                    # fimg_dir = os.path.basename(os.path.dirname(img_path))
                    # fimg_dir = Path(img_path).parent.name
                    # 不带子目录
                    fimg_dir = None
                    # fimg_name = os.path.basename(img_path)
                    fimg_name = Path(img_path).name

                    if fimg_dir is not None:
                        # fimg_path = os.path.join(ftpconfig["ftp_uploaddir"], fimg_dir, fimg_name).replace("\\", '/')
                        fimg_path = ftpconfig["ftp_uploaddir"] / fimg_dir / fimg_name
                        try:
                            ftp.cwd(fimg_dir)
                        except error_perm as e:
                            if "550" in str(e):
                                try:
                                    ftp.mkd(fimg_dir)
                                    ftp.cwd(fimg_dir)
                                except Exception as mkd_err:
                                    logger.error(f"无法创建或进入目录: {str(mkd_err)}")
                            else:
                                logger.error(f"无法进入目录，错误详情: {str(e)}")
                    else:
                        # fimg_path = os.path.join(ftpconfig["ftp_uploaddir"], fimg_name).replace("\\", '/')
                        fimg_path = ftpconfig["ftp_uploaddir"] / fimg_name
                    with open(img_path, 'rb') as sf:
                        ftp.storbinary(f'STOR {fimg_path}', sf, 1024)
                    break
                except:
                    if i == self.RETRY_TIMES - 1:
                        return False
                    else:
                        time.sleep(self.RETRY_DELAY)
                finally:
                    if ftp:
                        ftp.quit()
        return True

    # 发布内容模式
    def publish_content_data(self, siteconfig: dict, article: dict):
        # 优化网络，使用本地文件保存需要抓取的网址,本地保存文章文件（txt/markdown文档）
        if self.run_mode["filename"] == "md5":
            _fn = md5(article["title"].encode()).hexdigest()
        elif self.run_mode["filename"] == "title":
            _fn = re.sub(r'[?/\\:"<>*^| ]', "", article["title"])
        else:
            _fn = None
        if _fn is not None:
            filename = f'{_fn}.md' if self.run_mode["extend"] == "hexo" else f'{_fn}.txt'
            file_path = os.path.join(self.cur_dir, "content", filename)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            # 写入本地文件保存
            with open(file_path, 'w+', encoding='utf-8') as sw:
                if self.run_mode["extend"] == "hexo":
                    sw.write(f'{ArticlePublish(article_info=article).convert_hexo_content()}')
                elif self.run_mode['extend'] == "shuimiao":
                    sw.write(f'{article["title"]}\n{article["content"]}')
                else:
                    sw.write(f'{article["content"]}')
                if os.path.exists(file_path):
                    fileFlag = f'生成成功'
                else:
                    fileFlag = f'生成失败'

        # 远程发布文章内容数据（各类CMS）
        if self.run_mode["publish"] and self.run_mode["run"] == "Production":
            # 远程发布并返回状态
            flag = ArticlePublish(siteconfig, article).publish_to_webcms()
            if "成功" in flag["status"] or "存在" in flag["status"]:
                if self.run_mode["database"] == "txt":
                    with open(self.publish, 'a+', encoding='utf-8') as pf:
                        pf.write(f'{article["source"]}\n ')
                else:
                    self.database.update_data(
                        self.tname,
                        {'publish': 1, 'title': article["title"], 'tags': json.dumps(article["tags"])},
                        f'source=\'{article["source"]}\''
                    )

                return dict(**flag)
            else:
                return {'status': '发布失败'}
        else:
            return {'title': article["title"], 'status': fileFlag}


"""文章发布类"""
class ArticlePublish(object):
    def __init__(self, site_parms: dict = None, article_info: dict = None):
        self.site_parms = site_parms
        self.article_info = article_info
        self.headers = {
            'Referer': self.site_parms["web_config"]["web_domain"] if site_parms else '',
            'Host': urlparse(self.site_parms["web_config"]["web_domain"]).netloc if site_parms else '',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
        }

    # TF-IDF提取关键词
    def extract_keywords(self, article_content: str, segk_list: dict = None):
        # 去除标签和符号
        # content = re.sub('<[^>]+>|[^\w\s]', '', article_content)
        content = BeautifulSoup(re.sub('<[^>]+>', '', article_content), 'lxml').get_text()

        # 加载依赖字典库
        stop_words, require_words = segk_list["filter"], segk_list["needed"]
        # 使用列表解析添加自定义词汇
        [jieba.add_word(_) for _ in require_words]

        if stop_words is not None and len(stop_words) > 10:
            # 分词并去除停用词
            # https://blog.csdn.net/Metal1/article/details/85489724/
            # https://blog.csdn.net/Vensmallzeng/article/details/99694847
            # 精确分词，生成 list
            words = [word for word in jieba.lcut(content) if word not in stop_words]
            # 搜索引擎分词，生成 list
            # words = [word for word in jieba.cut_for_search(content) if word not in stop_words]
        else:
            words = [word for word in jieba.cut_for_search(content)]

        if require_words is not None and len(require_words) > 10:
            # 使用TF-IDF算法提取关键词
            keywords = jieba.analyse.extract_tags(' '.join(words), topK=500, withWeight=True, allowPOS=())
            # 按关键词长度和权重进行排序
            keywords.sort(key=lambda x: (len(x[0]), x[1]), reverse=True)
            # 过滤出需要的关键词并提取权重最高的五个关键词
            # 从排序后的关键词中选取至少一个在 require_words 中的关键词
            t_k = [keyword for keyword, _ in keywords if keyword in require_words]
            # 如果未找到满足 require_words 的关键词，则直接选取前 10 个关键词
            if not t_k:
                t_k = [keyword for keyword, _ in keywords]
        else:
            # 使用TF-IDF算法提取关键词
            keywords = jieba.analyse.extract_tags(' '.join(words), topK=500, withWeight=True, allowPOS=())
            # 按关键词长度和权重进行排序
            # keywords.sort(key=lambda x: x[1], reverse=True)
            keywords.sort(key=lambda x: (len(x[0]), x[1]), reverse=True)
            # 过滤出需要的关键词并提取权重最高的五个关键词
            t_k = [keyword for keyword, _ in keywords]

        # 过滤纯符号、长度小于2的纯数字、纯百分数和纯浮点数关键词
        return [k for k in t_k if not re.match(r'^[\W_]*$', k) and len(k) >= 2 and not re.match(r'^\d+(\.\d+)?%?$', k)]
        # return top_keywords

    # 中文标题URL转拼音URL，方便Google
    def chinese_url_slug(self):
        if self.site_parms["web_config"]["web_sluglang"] == "english":
            translator = Translator(to_lang="en", from_lang="zh")
            remove_pm = translator.translate(self.article_info["title"])
            if self.site_parms["web_config"]["web_slugsymbol"] is not None:
                _ = remove_pm.replace(' ', self.site_parms["web_config"]["web_slugsymbol"])
            else:
                _ = remove_pm.replace(' ', '-')
            return re.sub(r'-+', '-', _)
        elif self.site_parms["web_config"]["web_sluglang"] == "pinyin":
            # 使用 pypinyin 将中文标题转换为拼音列表
            pinyin_list = pinyin(self.article_info["title"], style=Style.NORMAL)
            # 将拼音列表中的每个拼音词组转换为字符串，并去除标点符号,但保留-
            pinyin_title = ' '.join([''.join(word) for word in pinyin_list])
            remove_pm = re.sub('[^\w\s]', '', pinyin_title)
            if self.site_parms["web_config"]["web_slugsymbol"] is not None:
                _ = remove_pm.replace(' ', self.site_parms["web_config"]["web_slugsymbol"])
            else:
                _ = remove_pm.replace(' ', '-')
            return re.sub(r'-+', '-', _)
        else:
            return

    # 内容转换为Hexo文档格式（.md）
    def convert_hexo_content(self):
        if self.article_info['title']:
            self.article_info['title'] = self.article_info['title'].replace("'", "\\'").replace('"', '\\"')
        if self.article_info['excerpt']:
            self.article_info['excerpt'] = self.article_info['excerpt'].replace("'", "\\'").replace('"', '\\"')
        # title, date, tags, categories, content
        yaml_front_matter = f"---\n"
        yaml_front_matter += f"title: \"{self.article_info['title']}\"\n"
        yaml_front_matter += f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"

        if not self.article_info["tags"] or self.article_info["tags"] == ['']:
            pass
        else:
            yaml_front_matter += "tags:\n"
            for tag in self.article_info["tags"]:
                yaml_front_matter += f"  - {tag}\n"

        if isinstance(self.article_info["category"], list):
            yaml_front_matter += "categories:\n"
            for category in self.article_info["category"]:
                yaml_front_matter += f"  - {category}\n"

        yaml_front_matter += "---\n"
        return yaml_front_matter + h.handle(''.join([str(p) for p in self.article_info["content"]]))

    # 推送链接到百度
    def push_to_baidu(self, permalink: str):
        web_domain = self.site_parms["web_config"]["web_domain"]
        baidu_token = self.site_parms["push_config"]["baidu_token"]
        baidu_api_url = f'http://data.zz.baidu.com/urls?site={web_domain}&token={baidu_token}'
        # 解决问题的方法！！！防止data传参的中文乱码！或在post下面专门设置
        rs = json.loads(requests.post(baidu_api_url, data=permalink.encode('utf-8')).content)
        if 'success' in rs.keys():
            if 'not_same_site' in rs.keys():
                if len(rs['not_same_site']) != 0:
                    text = f'推送失败：推送域名与绑定域名不一致'
                else:
                    text = f'推送失败：页面抓取不到合法URL链接'
            else:
                text = f"本次成功推送{rs['success']}条，今日剩余配额{rs['remain']}条"
        elif 'error' in rs.keys():
            if rs['message'] == 'site error':
                text = f'推送失败：站点未在站长平台验证'
            elif rs['message'] == 'empty content':
                text = f'推送失败：推送内容为空'
            elif rs['message'] == 'only 2000 urls are allowed once':
                text = f'推送失败：每次最多只能提交2000条链接'
            elif rs['message'] == 'over quota':
                text = f'推送失败：超过每日配额了'
            elif rs['message'] == 'token is not valid':
                text = f'推送失败：超过每日配额了，超配额后再提交都是无效的'
            elif rs['message'] == 'not found':
                text = f'推送失败：接口地址填写错误'
            elif rs['message'] == 'internal error, please try later':
                text = f'推送失败：服务器偶然异常，通常重试就会成功'
            else:
                text = f'推送失败：接口服务器未知错误'
        else:
            text = f'推送失败，请检查原因！'
        # return permalink, text
        return [{'link': permalink, 'message': text}]

    #推送国外搜索引擎
    def push_to_indexnow(self, permalink: str, engine: str = None):
        # 解决问题的方法！！！防止data传参的中文乱码！或在post下面专门设置
        # rs = json.loads(requests.post(indexnow_url, data=permalink.encode('utf-8')).content)
        web_domain = self.site_parms["web_config"]["web_domain"]
        indexnow_key = self.site_parms["push_config"]["indexnow_key"]
        indexnow_key_location = self.site_parms["push_config"]["indexnow_key_location"]
        searchengine = {
            'indexnow': 'https://api.indexnow.org/indexnow',
            'yandex': 'https://yandex.com/indexnow',
            'bing': 'https://www.bing.com/indexnow',
            'naver': 'https://searchadvisor.naver.com/indexnow',
            'seznam': 'https://search.seznam.cz/indexnow'
        }
        api_url = searchengine[engine.lower()] if engine else searchengine[random.choice(list(searchengine.keys()))]
        data = {
            'host': urlparse(web_domain).netloc,
            'key': indexnow_key,
            'keyLocation': indexnow_key_location,
            'urlList': [permalink],
        }
        try:
            res = requests.post(
                api_url,
                headers={'Content-Type': 'application/json', 'Host': urlparse(api_url).netloc},
                data=json.dumps(data)
            )
            if res.status_code == 200:
                return [{'link': permalink, 'message': f"推送成功"}]
            else:
                return self.push_to_indexnow(permalink, engine)
        except Exception as e:
            if 'HTTPSConnectionPool' in str(e):
                return self.push_to_indexnow(permalink, engine)
            else:
                return [{'link': permalink, 'message': f"推送失败，异常情况：{str(e)}"}]

    # 发布文章到WordPress网站
    def post_data_wordpress(self):
        """需要WordPress后台对应用户下设置应用程序密码"""
        web, tag_ids, media_ids = self.site_parms["web_config"], [], []
        auth = {
            'Authorization': 'Basic ' + b64encode(f'{web["web_username"]}:{web["web_apikey"]}'.encode()).decode()
        }
        # 获取标签ID列表
        for tag_name in self.article_info["tags"]:
            try:
                # /wp-json/wp/v2
                _ = requests.get(
                    f'{web["web_domain"]}{web["web_apiurl"]}/tags?search={tag_name}',
                    headers=dict(**self.headers, **auth)
                )
                if _.status_code == 200:
                    tag_ids.append(_.json()[0]["id"])
            except:
                tag = {
                    'name': tag_name,
                    'slug': md5(f'{tag_name}'.encode()).hexdigest()
                }
                try:
                    _ = requests.post(
                        f'{web["web_domain"]}{web["web_apiurl"]}/tags',
                        json=dict(list(tag.items())),
                        headers=dict(**self.headers, **{'Content-Type': 'application/json'}, **auth)
                    )
                    if _.status_code == 201:
                        tag_ids.append(_.json()["id"])
                        # tag_ids.append(_.json()["data"]["term_id"])
                except Exception as e:
                    logger.error(f'标签新增失败{str(e)}')
        # 上传图片获取ID
        if web["web_library"]:
            # 先上传图片到媒体库，如果有图片的话
            if "thumblocal" in self.article_info and len(self.article_info["thumbnail"]) > 0:
                # 遍历上传所有图片，带上序号
                for index, imgpath in enumerate(self.article_info["thumblocal"]):
                    try:
                        with open(imgpath, 'rb') as image_file:
                            # 获取图片文件名称和拓展名
                            if web["web_webp"]:
                                # 强制转换成WEBP图片
                                image = Image.open(image_file)
                                webp_data = BytesIO()
                                image.convert('RGB').save(webp_data, 'WEBP')
                                webp_data.seek(0)  # 将光标移回到数据开头
                                image_data = webp_data.read()
                                image_filename = f'{os.path.splitext(os.path.basename(imgpath))[0]}.webp'
                                content_type = 'image/webp'
                            else:
                                image_data = image_file.read()
                                image_filename = os.path.basename(imgpath)
                                content_type = mimetypes.guess_type(image_filename)[0]

                            # logger.info(Image.open(BytesIO(image_data)).format)
                            _ = requests.post(
                                f'{web["web_domain"]}{web["web_apiurl"]}/media',
                                data=image_data,
                                headers=dict(
                                    **self.headers,
                                    **{
                                        'Content-Type': content_type,
                                        'Content-Disposition': f'attachment; filename={image_filename}'
                                    },
                                    **auth
                                )
                            )
                            if _.status_code == 201:
                                media_ids.append(_.json()["id"])
                            else:
                                logger.error(f'上传图片时失败: {_.status_code}')
                    except Exception as e:
                        logger.error(f'上传图片时发生异常: {str(e)}')
        # 发布相关文章数据
        post_data = {
            'categories': [self.article_info["category"]],
            'title': self.article_info["title"],
            'slug': self.chinese_url_slug(),
            # 'date': "2018-04-12 10:10:18",
            'content': self.article_info["content"],
            'tags': tag_ids if tag_ids else None,
            'excerpt': self.article_info["excerpt"],
            'status': "publish",
            'featured_media': media_ids[0] if media_ids else None,
        }
        try:
            post_res = requests.post(
                f'{web["web_domain"]}{web["web_apiurl"]}/posts',
                json=dict(list(post_data.items())),
                headers=dict(**self.headers, **{'Content-Type': 'application/json'}, **auth)
            )
            if post_res.status_code == 201:
                res = post_res.json()
                # del res["status"]
                if res["id"] is not None and isinstance(res["id"], int):
                    permalink = res["link"]
                    res["baidu"] = self.push_to_baidu(permalink)
                    res["indexnow"] = self.push_to_indexnow(permalink, 'bing')
                    res["status"] = "发布成功"
                else:
                    res["baidu"] = list()
                    res["indexnow"] = list()
                    res["status"] = "发布失败"
            else:
                res = {
                    'baidu': list(),
                    'indexnow': list(),
                    'status': f'发布失败, {post_res.status_code}'
                }

            return dict(
                **self.article_info,
                **{'baidu': res["baidu"], 'indexnow': res["indexnow"], 'status': res["status"]}
            )
        except Exception as e:
            return dict(
                **self.article_info,
                **{'status': f'发布失败，异常信息：{str(e)}'}
            )

    # 发布文章到Z-BlogPHP网站
    def post_data_zblog(self):
        """需要z-blog后台开启API"""
        web = self.site_parms["web_config"]
        login_data = {
            'username': web["web_username"],
            'password': md5(web["web_password"].encode("utf-8")).hexdigest(),
            'savedate': 0
        }
        try:
            # /zb_system/api.php
            login_res = requests.post(
                f'{web["web_domain"]}{web["web_apiurl"]}?mod=member&act=login',
                headers=dict(**{'Content-Type': 'application/json'}, **self.headers),
                json=login_data
            )
            if login_res.status_code == 200:
                res_json = login_res.json()
                """
                # 上传附件
                upload_data = {

                }
                try:
                    upload_res = requests.post(
                        f'{web["web_domain"]}{web["web_apiurl"]}?mod=upload&act=post',
                        headers=dict(
                            **{'Authorization': f'Bearer {res_json["data"]["token"]}'},
                            **{'Content-Type': 'application/json'},
                            **self.headers
                        ),
                        json={'file': open(f'E:\\Develop\\PyTask\\楚莺百科\\www.baikew.net\\uploads\\00fe651ef2d9b41c8ee22a231eb723fb.jpg', 'rb').read()}
                    )
                    if upload_res.status_code == 200:
                        _ = upload_res.json()
                        if "操作成功" in _["message"]:
                            media_id = upload_res.json()["upload"]["ID"]
                        else:
                            logger.error(f'图片上传失败')
                    else:
                        logger.error(f'图片上传失败,{str(upload_res.status_code)}')
                except Exception as e:
                    logger.error(f'图片上传失败{str(e)}')
                """
                # 文章发布
                post_data = {
                    'ID': 0,
                    'Type': 0,
                    'Status': 0,
                    'Title': self.article_info["title"],
                    'CateID': self.article_info["category"],
                    'Intro': self.article_info["excerpt"],
                    'Content': self.article_info["content"],
                    'Tag': ', '.join(self.article_info["tags"]),
                }
                try:
                    post_res = requests.post(
                        f'{web["web_domain"]}{web["web_apiurl"]}?mod=post&act=post',
                        headers=dict(
                            **{'Authorization': f'Bearer {res_json["data"]["token"]}'},
                            **dict(**{'Content-Type': 'application/json'}, **self.headers)
                        ),
                        json=post_data
                    )
                    if post_res.status_code == 200:
                        res = post_res.json()
                        if res["data"]["post"]["ID"] is not None and isinstance(res["data"]["post"]["ID"], int):
                            permalink = res["data"]["post"]["Url"]
                            res["baidu"] = self.push_to_baidu(permalink)
                            res["indexnow"] = self.push_to_indexnow(permalink, 'bing')
                            res["status"] = f'发布成功'
                        else:
                            res["baidu"] = list()
                            res["indexnow"] = list()
                            res["status"] = f'发布失败：{post_res.status_code}'
                    else:
                        res = {
                            'baidu': list(),
                            'indexnow': list(),
                            'status': f'发布失败, {post_res.status_code}'
                        }

                    return dict(
                        **self.article_info,
                        **{'baidu': res["baidu"], 'indexnow': res["indexnow"], 'status': res["status"]}
                    )
                except Exception as e:
                    return {'status': f'发布失败，异常原因：{str(e)}'}
            else:
                return {'status': f'API鉴权认证失败， {login_res.status_code}'}
        except Exception as e:
            return {'status': f'登录失败，异常原因：{str(e)}'}

    # 发布文章到Emlog网站
    def post_data_emlog(self):
        """需要Emlog后台开启API"""
        web = self.site_parms["web_config"]
        _timestamp = str(int(time.time()))
        signature = md5(f'{_timestamp}{web["web_apikey"]}'.encode()).hexdigest()
        auth = {
            'req_time': _timestamp,
            'req_sign': signature
        }
        payload = {
            'sort_id': self.article_info["category"],
            'title': self.article_info["title"],
            # 'author_uid': 1,
            # 'post_date': "2018-04-12 10:10:18",
            # 'cover': self.article_info.get("thumbnail", [None])[0],
            'content': self.article_info["content"],
            'tags': ','.join(self.article_info["tags"]),
            'excerpt': self.article_info["excerpt"],
            'draft': 'n',
        }
        data = dict(list(payload.items()) + list(auth.items()))
        try:
            # ?rest-api
            response = requests.post(
                f'{web["web_domain"]}{web["web_apiurl"]}=article_post',
                data=data,
                headers=self.headers
            )
            if response.status_code == 200:
                res = response.json()
                if res["msg"] == "ok" and res["data"] is not None and isinstance(res["data"]["article_id"], int):
                    res["status"] = "发布成功"
                    permalink = f'{web["web_domain"]}/?post={res["data"]["article_id"]}'
                    res["baidu"] = self.push_to_baidu(permalink)
                    res["indexnow"] = self.push_to_indexnow(permalink, 'bing')
                else:
                    res["baidu"] = list()
                    res["indexnow"] = list()
                    res["status"] = "发布失败"
            else:
                res = {
                    'baidu': list(),
                    'indexnow': list(),
                    'status': f"发布失败"
                }

            return dict(
                **self.article_info,
                **{'baidu': res["baidu"], 'indexnow': res["indexnow"], 'status': res["status"]}
            )
        except Exception as e:
            return {'status': f'发布失败，异常信息：{str(e)}'}

    # 发布文章到ssycms/ugccms
    def post_data_ssycms(self):
        """需要SSYCMS/UGCCMS后台开启火车头插件"""
        web = self.site_parms["web_config"]
        payload = {
            'title': self.article_info["title"],
            'content': self.article_info["content"],
            'cid': self.article_info["category"],
            'tags': ','.join(self.article_info["tags"]),
            'keywords': ','.join(self.article_info["tags"]),
            # "description": "",
            # "dpublish_time": "",
            # "img_url": self.article_info.get("thumbnail", [None])[0],
            # 'password': f'{web["web_apikey"]}',
        }
        if "thumbnail" in self.article_info:
            img_url = {'img_url': self.article_info["thumbnail"][0]}
        else:
            _ = Translator(to_lang="en", from_lang="zh").translate(random.choice(self.article_info["tags"]))
            featured_media = requests.get(f"https://source.unsplash.com/1600x900/?{_}").url
            img_url = {'img_url': featured_media}
        try:
            # /huochetou/ApiUserHuochetou/articleAdd
            res = requests.post(
                f'{web["web_domain"]}{web["web_apiurl"]}',
                data=urlencode(dict(**payload, **img_url, **{'password': f'{web["web_apikey"]}'})),
                headers=dict(**{'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'}, **self.headers)
            )
            return dict(**self.article_info, **{'status': res.text})
        except Exception as e:
            return {'status': f'发布失败，异常信息：{str(e)}'}

    # 发布文章到小旋风蜘蛛池
    def post_data_xxfseo(self):
        """需要上传xxffabu.php接口"""
        web = self.site_parms["web_config"]
        payload = {
            'title': self.article_info['title'],
            'content': self.article_info['content']
        }
        try:
            # xxfapi.php
            res = requests.post(
                f'{web["web_domain"]}/{web["web_apiurl"]}',
                data=urlencode(payload),
                params={'token': f'{web["web_apikey"]}'},
                headers=dict(**{'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'}, **self.headers)
            )
            return dict(**self.article_info, **{'status': res.text})
        except Exception as e:
            return {'status': f'发布失败，异常信息：{str(e)}'}

    # 发布文章到PbootCMS
    def post_data_pbootcms(self):
        """需要修改API模块，开启API验证"""
        web = self.site_parms["web_config"]
        _timestamp = str(int(time.time()))
        _ = f'{web["web_username"]}{web["web_apikey"]}{_timestamp}'
        signature = md5((md5(_.encode()).hexdigest()).encode()).hexdigest()
        auth = {
            'appid': web["web_username"],
            'timestamp': _timestamp,
            'signature': signature
        }
        payload = {
            # 'acode': "cn",
            'scode': self.article_info["category"],
            'title': self.article_info["title"],
            # 'author': web["web_username"],
            # 'date': "2018-04-12 10:10:18",
            # 'ico': self.article_info.get("thumbnail", [None])[0],
            'content': self.article_info["content"],
            'tags': ','.join(self.article_info["tags"]),
            # 'keywords': ','.join(self.article_info["tags"]),
            'description': self.article_info["excerpt"],
            # 'status': 0,
            # 'subtitle': "",
            # 'filename': self.chinese_url_slug(),
            # 'source': "",
            # 'outlink': "",
            # 'pics': "图集",
            # 'picstitle': "",
            # 'enclosure': "",
            # 'sorting': "",
            # 'istop': "",
            # 'isrecommend': "",
            # 'isheadline': "",
            # 'gid': "",
            # 'gtype': "",
            # 'gnote': "",
            # 'create_user': "",
            # 'update_user': ""
        }
        data = dict(list(payload.items()) + list(auth.items()))
        try:
            # /api.php/cms/add
            response = requests.post(
                f'{web["web_domain"]}{web["web_apiurl"]}',
                data=urlencode(data),
                headers=dict(**{'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'}, **self.headers)
            )
            if response.status_code == 200:
                res = response.json()
                if "成功" in res["data"] and res["tourl"] is not None and isinstance(res["tourl"], int):
                    res["status"] = "发布成功"
                else:
                    res["status"] = "发布失败"
            else:
                res = {'status': f'发布失败'}

            return dict(**self.article_info, **{'status': res["status"]})
        except Exception as e:
            return {'status': f'发布失败，异常信息：{str(e)}'}

    # 发布文章到JpressCMS
    def post_data_jpress(self):
        """需要修改API模块，开启API验证"""
        web = self.site_parms["web_config"]
        _timestamp = str(int(time.time()))
        _ = f'{web["web_username"]}{web["web_apikey"]}{_timestamp}'
        signature = md5((md5(_.encode()).hexdigest()).encode()).hexdigest()
        auth = {
            'appid': web["web_username"],
            'timestamp': _timestamp,
            'signature': signature
        }
        payload = {
            # 'acode': "cn",
            'scode': self.article_info["category"],
            'title': self.article_info["title"],
            # 'author': web["web_username"],
            # 'date': "2018-04-12 10:10:18",
            # 'ico': self.article_info.get("thumbnail", [None])[0],
            'content': self.article_info["content"],
            'tags': ','.join(self.article_info["tags"]),
            # 'keywords': ','.join(self.article_info["tags"]),
            'description': self.article_info["excerpt"],
            # 'status': 0,
            # 'subtitle': "",
            # 'filename': self.chinese_url_slug(),
            # 'source': "",
            # 'outlink': "",
            # 'pics': "图集",
            # 'picstitle': "",
            # 'enclosure': "",
            # 'sorting': "",
            # 'istop': "",
            # 'isrecommend': "",
            # 'isheadline': "",
            # 'gid': "",
            # 'gtype': "",
            # 'gnote': "",
            # 'create_user': "",
            # 'update_user': ""
        }
        data = dict(list(payload.items()) + list(auth.items()))
        try:
            # /api/article/doCreate
            response = requests.post(
                f'{web["web_domain"]}{web["web_apiurl"]}',
                data=urlencode(data),
                headers=dict(**{'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'}, **self.headers)
            )
            if response.status_code == 200:
                res = response.json()
                if "成功" in res["data"] and res["tourl"] is not None and isinstance(res["tourl"], int):
                    res["status"] = "发布成功"
                else:
                    res["status"] = "发布失败"
            else:
                res = {'status': f'发布失败'}

            return dict(**self.article_info, **{'status': res["status"]})
        except Exception as e:
            return {'status': f'发布失败，异常信息：{str(e)}'}

    # 总控制程序
    def publish_to_webcms(self):
        # 获取CMS名称，选取对应的发布接口
        cms = self.site_parms["web_config"]["web_cmsname"].lower()
        if cms == 'WordPress'.lower():
            return self.post_data_wordpress()
        elif cms == 'Z-Blog'.lower():
            return self.post_data_zblog()
        elif cms == 'Emlog'.lower():
            return self.post_data_emlog()
        elif cms == 'Discuz!X'.lower():
            return self.post_data_discuzx()
        elif cms == 'Dedecms'.lower():
            return self.post_data_dedecms()
        elif cms == 'Empirecms'.lower():
            return self.post_data_empirecms()
        elif cms == 'Xunruicms'.lower():
            return self.post_data_xunruicms()
        elif cms == 'Eyoucms'.lower():
            return self.post_data_eyoucms()
        elif cms == 'Discuz!Q'.lower():
            return self.post_data_discuzq()
        elif cms == 'phpcms'.lower():
            return self.post_data_phpcms()
        elif cms == 'ssycms' or cms == 'ugccms':
            return self.post_data_ssycms()
        elif cms == 'phpmps'.lower():
            return self.post_data_phpmps()
        elif cms == 'mayicms'.lower():
            return self.post_data_mayicms()
        elif cms == 'xxfseo'.lower():
            return self.post_data_xxfseo()
        elif cms == 'Pbootcms'.lower():
            return self.post_data_pbootcms()
        elif cms == 'Jpress'.lower():
            return self.post_data_jpress()
        else:
            return


"""数据操作类"""
class ArticleDataBase(object):
    def __init__(self, db_type: str, db_config: dict = None):
        """初始化，连接到指定的数据库"""
        if db_type == 'mysql':
            try:
                self.conn = mysql.connector.connect(db_config, autocommit=True, connect_timeout=300)
            except mysql.connector.Error as err:
                logger.error(f"MySQL连接错误: {err}")
                raise
        elif db_type == 'sqlite':
            self.conn = sqlite3.connect(database=f'./{db_config["database"]}', check_same_thread=False)
            self.cursor = self.conn.cursor()
        else:
            raise ValueError("不支持的数据库类型")
        self.db_type = db_type
        self.cursor = self.conn.cursor()

    def create_table(self, table_name: str, columns: str):
        """创建表格"""
        sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({columns});"
        try:
            self.cursor.execute(sql)
            self.conn.commit()
        except Exception as e:
            logger.error(f'数据库创建失败，异常情况：{str(e)}')

    def drop_table(self, table_name: str):
        """删除表格"""
        sql = f"DROP TABLE IF EXISTS {table_name};"
        try:
            self.cursor.execute(sql)
            self.conn.commit()
        except Exception as e:
            logger.error(f'数据库删除失败，异常情况：{str(e)}')

    def insert_data(self, table_name: str, data: dict):
        """插入数据"""
        columns = ', '.join(data.keys())
        # 创建与参数数量相同的问号占位符
        if self.db_type == 'mysql':
            placeholders = ', '.join(['%s'] * len(data))
            sql = f"INSERT IGNORE INTO {table_name} ({columns}) VALUES ({placeholders});"
        else:
            placeholders = ':' + ', :'.join(data.keys())
            sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders});"
        try:
            if self.db_type == 'mysql':
                self.cursor.execute(sql, tuple(data.values()))
            else:
                self.cursor.execute(sql, data)
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f'数据插入失败，异常情况：{str(e)}')

    def update_data(self, table_name: str, data: dict, condition: str = None):
        """更新数据"""
        if self.db_type == 'mysql':
            set_clause = ', '.join(f"{key} = %s" for key in data.keys())
        else:
            set_clause = ', '.join(f"{key} = ?" for key in data.keys())
        if condition is not None:
            sql = f"UPDATE {table_name} SET {set_clause} WHERE {condition};"
        else:
            sql = f"UPDATE {table_name} SET {set_clause};"
        try:
            self.cursor.execute(sql, tuple(data.values()))
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(f'数据更新失败，回滚事务：{str(e)}')

    def delete_data(self, table_name: str, condition: str):
        """删除数据"""
        sql = f"DELETE FROM {table_name} WHERE {condition};"
        try:
            self.cursor.execute(sql)
            self.conn.commit()
        except Exception as e:
            logger.error(f'数据删除失败，异常情况：{str(e)}')

    def select_data(self, table_name: str, columns: str = None, condition: str = None):
        """查询数据"""
        if columns is None:
            columns = '*'
        if condition is None:
            condition = ''
        else:
            condition = f"WHERE {condition}"
        sql = f"SELECT {columns} FROM {table_name} {condition};"
        try:
            self.cursor.execute(sql)
            return self.cursor.fetchall()
        except Exception as e:
            logger.error(f'数据查询失败，异常情况：{str(e)}')

    def __del__(self):
        """关闭数据库连接"""
        if self.db_type == 'mysql':
            if hasattr(self, 'conn') and self.conn.is_connected():
                self.cursor.close()
                self.conn.close()
                logger.info(f"数据连接已关闭！")
        else:
            self.cursor.close()
            self.conn.close()
            logger.info(f"数据连接已关闭！")

