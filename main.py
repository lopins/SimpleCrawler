# -*- coding: utf-8 -*-
"""
# 远程采集发布助手：带采集和FTP上传功能
# 格式化导包顺序： pip install isort,然后：isort main.py
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import requests
from Controller import ArticleCollector

# 启动函数
def main():
    # 读取 JSON配置 文件[标准库不能带注释]
    _ = json.load(open('configs.json', 'r', encoding='utf-8'))["data"]["itemList"]
    web_configs = [site for site in _ if site["web_config"]["web_publish"]]


    # 分词所用依赖字典库
    kn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'RequireWords.txt')
    kf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stop_words.txt')
    # 读取需求词文件，如果不存在则设置为空
    if os.path.exists(kn_path):
        with open(kn_path, 'r', encoding='utf-8') as f:
            k_needed = f.read().splitlines()
    else:
        k_needed = []

    # 读取停用词文件,如果不存在则远程下载
    if os.path.exists(kf_path):
        with open(kf_path, 'r', encoding='utf-8') as f:
            k_filter = f.read().splitlines()
    else:
        base_domain = "https://{}.jsdelivr.net"
        subdomains = ["cdn", "fastly", "gcore"]
        path_suffix = "/gh/lopinv/owner-assets@gh-pages/docs/ChineseStopWords.txt"
        remote_stop_words = [base_domain.format(subdomain) + path_suffix for subdomain in subdomains]
        rsw = None
        for swu in remote_stop_words:
            try:
                rsw = requests.get(url=swu, timeout=5)
                break
            except requests.exceptions.RequestException as e:
                print(str(e))
        if rsw is not None and rsw.status_code == 200:
            with open(kf_path, 'wb') as rs:
                rs.write(rsw.content)
            with open(kf_path, 'r', encoding='utf-8') as f:
                k_filter = f.read().splitlines()

    # 脚本模式常规参数
    run_mode = {
        "nlpsplit": False,    # 是否强制分词
        "forbid": False,  # 是否开启禁用词模式    True/False
        "imgprefix": f"https://www.lopins.cn/wp-content/uploads",  # 图片前缀网址，可以是相对路径，也可是绝对路径
        "recrawl": False,  # 是否重新抓取所有连接    True/False
        "threads": 10,  # 是否多线程抓取
        "garble": True, # 是否过滤乱码
        "database": "mysql",  # 爬取链接记录，txt/mysql
        "filename": "",  # 保存文件名模式 ： title/md5, 是否本地存储文章文件,如不需要本地文件，请设置为空
        "extend": "hexo",  # 第三方发布器名称 shuimiao:水淼发布器txt格式，hexo:Hexo博客Markdown格式
        "publish": True,  # 是否远程发布    True/False
        "run": "Production"  # 程序模式   Debug/Production
    }

    # 日志数据库
    db_config = {
        "host": '127.0.0.1', # # 数据库地址
        "port": 3306, # 数据库端口
        "user": 'root', # 数据库账户
        "password": '1207', # 数据库密码
        "database": '2dea6a19bdca383b', # 使用数据库
        "charset": "utf8mb4" # 数据库编码
    }

    """需要爬取的远程站点信息"""
    # 需要处理的基础信息
    base_url = f"https://www.baikew.net"
    if run_mode["run"] == "Debug":
        cat_list = [f'{base_url}{url}' for url in [f'/shcs/p{n}.html' for n in range(1, 10)]]
    else:
        cat_list = [f'{base_url}{url}' for url in
            [f'/shcs/p{n}.html' for n in range(1, 1817)] + \
            [f'/xzys/p{n}.html' for n in range(1, 1683)] + \
            [f'/mstj/p{n}.html' for n in range(1, 578)] + \
            [f'/etjy/p{n}.html' for n in range(1, 277)] + \
            [f'/lygl/p{n}.html' for n in range(1, 174)] + \
            [f'/jkzs/p{n}.html' for n in range(1, 3658)] + \
            [f'/nxss/p{n}.html' for n in range(1, 1017)] + \
            [f'/rmys/p{n}.html' for n in range(1, 50)] + \
            [f'/dftc/p{n}.html' for n in range(1, 169)]
        ]
    # 网站列表页链接和详情页标题CSS定位器
    web_selectors = {
        "list": {
            "link": f"div.lists > div.list > a"
        },
        "content": {
            "title": f"div.detailTitle > h1",
            "category": f"",
            "tags": f"",
            "excerpt": f"",
            "content": f"div.detailText"
        }
    }
    # 需要过滤掉的标签选择器
    remove_selectors = {
        "tags": [
            f'div[class="showTime"]'
        ],
        "regex": [
            r'<script(.*?)><\/script>'
        ]
    }
    # 图片上水印文字
    watermark = ''
    # 需求词，只保留包含这些词的内容
    filter_titles = []
    # 排除词，不保留包含这些词的内容
    bad_words = []
    """需要爬取的远程站点信息"""


    # 构建参数字典
    site_args = {
        "web_configs": web_configs,
        "base_url": base_url,
        "cat_list": cat_list,
        "web_selectors": web_selectors,
        "remove_selectors": remove_selectors,
        "watermark": watermark,
        "filter_titles": filter_titles,
        "bad_words": bad_words,
        "segk_list": {
            'filter': k_filter,
            'needed': k_needed
        },
        "run_mode": run_mode,
        "db_config": db_config
    }

    # 开始爬取处理数据
    print(f'参数加载完毕！开始运行......')
    # 使用ThreadPoolExecutor创建线程池
    # with ThreadPoolExecutor(max_workers=run_mode["threads"]) as executor:
    #     # # 提交获取文章列表任务
    #     if run_mode["recrawl"]:
    #         executor.submit(ArticleCollector(site_args).get_post_list)
    #     # 检查是否需要发布任务
    #     if run_mode["publish"]:
    #         executor.submit(ArticleCollector(site_args).handle_post_data)
    ArticleCollector(site_args).handle_post_data()

if __name__ == '__main__':
    main()
