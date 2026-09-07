import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(PROJECT_DIR / "card-bot" / "tg-monitor"))

from pan123_link_parser import extract_123_links, parse_123_link


def test_plain_link_with_text_code():
    items = extract_123_links("资源 https://www.123pan.com/s/AbC12-xYz9 提取码：K8p2")
    assert items[0]["share_id"] == "AbC12-xYz9"
    assert items[0]["access_code"] == "K8p2"


def test_query_code_and_deduplication():
    text = "https://123865.com/s/AbC12?pwd=9x7k https://123865.com/s/AbC12?pwd=9x7k"
    items = extract_123_links(text)
    assert len(items) == 1
    assert items[0]["access_code"] == "9x7k"


def test_button_entity():
    class Entity:
        url = "https://www.123912.com/s/Zz99-kk88"

    items = extract_123_links("点击直达", [Entity()])
    assert items[0]["url"] == "https://www.123912.com/s/Zz99-kk88"


def test_dynamic_share_host_and_123pan_path():
    url = "https://1819070970.share.123pan.cn/123pan/MPrAjv-26sr3"
    items = extract_123_links(url)
    assert items == [{
        "url": url,
        "share_id": "MPrAjv-26sr3",
        "access_code": "",
        "host": "1819070970.share.123pan.cn",
    }]


def test_ignore_unsupported_or_invalid_links():
    assert parse_123_link("https://example.com/s/AbC12") == {}
    assert extract_123_links("https://123pan.com/file/AbC12") == []


if __name__ == "__main__":
    test_plain_link_with_text_code()
    test_query_code_and_deduplication()
    test_button_entity()
    test_dynamic_share_host_and_123pan_path()
    test_ignore_unsupported_or_invalid_links()
    print("123 LINK PARSER TESTS OK")
