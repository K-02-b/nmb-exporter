#!/usr/bin/env python3
"""
解析 X岛 cookie 二维码图片，将 cookie (userhash) 与 name 写入配置文件。

命令行用法:
    python parse_qrcode.py <二维码图片路径> [--config config.json]

Web UI 可 import:
    from parse_qrcode import decode_qr, parse_qr_cookie_file, parse_qr_cookie_raw

依赖（任选其一）：
    pip install pyzbar Pillow
    pip install opencv-python
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict


def decode_qr(image_path: str) -> str:
    """解析二维码，返回二维码内部字符串内容。优先 pyzbar，回退 opencv。"""
    raw = None

    # 方案 1: pyzbar
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode

        img = Image.open(image_path)
        results = decode(img)
        if results:
            raw = results[0].data.decode("utf-8")
    except ImportError:
        pass
    except Exception as e:
        print(f"[警告] pyzbar 解析失败: {e}")

    # 方案 2: opencv
    if not raw:
        try:
            import cv2

            img = cv2.imread(image_path)
            if img is None:
                raise RuntimeError("无法读取图片")

            detector = cv2.QRCodeDetector()
            data, _, _ = detector.detectAndDecode(img)
            if data:
                raw = data
        except ImportError:
            pass
        except Exception as e:
            print(f"[警告] opencv 解析失败: {e}")

    if not raw:
        raise RuntimeError("未能识别二维码内容，请确认图片有效，并已安装 pyzbar 或 opencv-python")

    return raw


def parse_qr_cookie_raw(raw: str) -> Dict[str, str]:
    """
    从二维码原始内容中解析 cookie/name。

    兼容两种格式：

    1. JSON:
        {"cookie": "...", "name": "..."}

    2. 直接是 userhash 字符串:
        abcdefghijklmn
    """
    raw = (raw or "").strip()
    if not raw:
        raise RuntimeError("二维码内容为空")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "cookie": raw,
            "name": "",
        }

    if not isinstance(info, dict):
        raise RuntimeError("二维码内容 JSON 格式不正确")

    cookie = info.get("cookie", "") or info.get("userhash", "")
    name = info.get("name", "")

    cookie = str(cookie).strip()
    name = str(name).strip()

    if not cookie:
        raise RuntimeError("二维码中未找到 cookie 字段")

    return {
        "cookie": cookie,
        "name": name,
    }


def parse_qr_cookie_file(image_path: str) -> Dict[str, str]:
    """解析二维码图片文件，返回 {'cookie': ..., 'name': ...}。"""
    raw = decode_qr(image_path)
    return parse_qr_cookie_raw(raw)


def main():
    parser = argparse.ArgumentParser(description="X岛 cookie 二维码解析工具")
    parser.add_argument("image", help="二维码图片路径")
    parser.add_argument("--config", default="config.json",
                        help="输出配置文件路径，默认 config.json")
    args = parser.parse_args()

    img_path = Path(args.image)
    if not img_path.exists():
        print(f"错误：图片不存在: {img_path}")
        sys.exit(1)

    try:
        raw = decode_qr(str(img_path))
        print(f"二维码原始内容: {raw}")

        info = parse_qr_cookie_raw(raw)
    except Exception as e:
        print(f"错误：{e}")
        sys.exit(1)

    cookie = info.get("cookie", "")
    name = info.get("name", "")

    # 合并已有配置（保留其他字段）
    config_path = Path(args.config)
    config = {}

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                config = {}
        except Exception:
            config = {}

    config["cookie"] = cookie
    config["name"] = name

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    print(f"\n已写入配置文件: {config_path}")
    print(f"  cookie : {cookie}")
    print(f"  name   : {name}")


if __name__ == "__main__":
    main()