#!/usr/bin/env python3
"""
AstrBot KK 插件源生成脚本
- 扫描上级目录中 astrbot_plugin_* 的插件
- 读取 metadata.yaml（基础信息）
- 并行抓取 GitHub API（stars, updated_at, logo）
- 生成 plugin_source.json + README.md

用法:
  python sync_kk_source.py
  python sync_kk_source.py --token ghp_xxx   # 遇到限速时用

首次运行会自动创建 .config 模板（可选填写）。
Token 获取: https://github.com/settings/tokens（public_repo 即可）
"""
import urllib.request
import urllib.error
import json
import os
import re
import sys
import argparse
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
SOURCE_FILE = os.path.join(BASE_DIR, "plugin_source.json")
README_FILE = os.path.join(BASE_DIR, "README.md")
CONFIG_FILE = os.path.join(BASE_DIR, ".config")

MAX_WORKERS = 10
REQUEST_TIMEOUT = 15
_PLUGIN_PATTERN = re.compile(r"^astrbot_plugin_")

_CLI_TOKEN = ""


def get_token():
    if _CLI_TOKEN:
        return _CLI_TOKEN
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if token and token != "your_token_here":
                        return token
    except (FileNotFoundError, OSError):
        pass
    return ""


def get_headers():
    headers = {"User-Agent": "astrbot-kkplugin-source"}
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_url(url):
    try:
        req = urllib.request.Request(url, headers=get_headers())
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        return resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            body = e.read().decode("utf-8", errors="replace")
            return None, dict(e.headers)
        return None, {}
    except Exception:
        return None, {}


def fetch_json(url):
    data, headers = fetch_url(url)
    if data:
        try:
            return json.loads(data), headers
        except json.JSONDecodeError:
            return None, headers
    return None, headers


def check_rate_limit(headers):
    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) == 0:
        print("!! GitHub API 速率限制已达上限。")
        print("!! 建议使用 --token ghp_xxx 或配置 .config 文件添加 Token 后重试。")
        print(f"!! Token 获取: https://github.com/settings/tokens (public_repo)")
        return True
    return False


def scan_plugin_dirs():
    plugins = []
    try:
        for entry in os.listdir(PARENT_DIR):
            full = os.path.join(PARENT_DIR, entry)
            if os.path.isdir(full) and _PLUGIN_PATTERN.match(entry):
                plugins.append(entry)
    except PermissionError:
        pass
    return sorted(plugins)


def read_metadata(plugin_dir):
    yaml_path = os.path.join(PARENT_DIR, plugin_dir, "metadata.yaml")
    if not os.path.exists(yaml_path):
        return None
    try:
        with open(yaml_path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None

    result = {"dir": plugin_dir}

    if HAS_YAML:
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                for key in ("name", "display_name", "author", "version", "desc", "repo", "astrbot_version"):
                    val = data.get(key)
                    if val:
                        result[key] = str(val)
                tags = data.get("tags")
                if tags and isinstance(tags, list):
                    result["tags"] = tags
                platforms = data.get("support_platforms")
                if platforms and isinstance(platforms, list):
                    result["support_platforms"] = platforms
                if "name" in result:
                    result["plugin_name"] = result.pop("name")
                return result
        except Exception:
            pass

    # Regex fallback
    for key in ("name", "display_name", "author", "version", "desc", "repo", "astrbot_version"):
        m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
        if m:
            val = m.group(1).strip().strip('"').strip("'")
            if val:
                result[key] = val
    if "name" in result:
        result["plugin_name"] = result.pop("name")
    platforms = []
    in_section = False
    for line in text.split("\n"):
        if re.match(r"^support_platforms:", line):
            in_section = True
            continue
        if in_section:
            m = re.match(r"^\s*-\s*(.+)$", line)
            if m:
                platforms.append(m.group(1).strip().strip('"').strip("'"))
            elif re.match(r"^\w+:", line):
                in_section = False
    if platforms:
        result["support_platforms"] = platforms
    return result


def parse_repo(repo_url):
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", repo_url)
    if match:
        return match.group(1), match.group(2).replace(".git", "")
    return None, None


def enrich_plugin(meta, rate_limited):
    display_name = meta.get("display_name", meta.get("plugin_name", meta["dir"]))
    author = meta.get("author", "")
    repo = meta.get("repo", f"https://github.com/{author}/{meta['dir']}")
    desc = meta.get("desc", "")

    entry = {
        "display_name": display_name,
        "desc": desc,
        "author": author,
        "repo": repo,
        "social_link": f"https://github.com/{author.split(' ')[0]}" if author else "",
    }

    tags = meta.get("tags")
    if tags:
        entry["tags"] = tags

    version = meta.get("version")
    if version:
        entry["version"] = version

    astrbot_ver = meta.get("astrbot_version")
    if astrbot_ver:
        entry["astrbot_version"] = astrbot_ver

    platforms = meta.get("support_platforms")
    if platforms:
        entry["support_platforms"] = platforms

    if rate_limited:
        return entry

    owner, repo_name = parse_repo(repo)
    if not owner or not repo_name:
        return entry

    api_url = f"https://api.github.com/repos/{owner}/{repo_name}"
    repo_data, headers = fetch_json(api_url)
    if check_rate_limit(headers):
        return entry
    if repo_data:
        stars = repo_data.get("stargazers_count")
        if stars is not None:
            entry["stars"] = stars
        updated = repo_data.get("pushed_at") or repo_data.get("updated_at")
        if updated:
            entry["updated_at"] = updated

    for branch in ("main", "master"):
        logo_url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/logo.png"
        try:
            req = urllib.request.Request(logo_url, method="HEAD", headers=get_headers())
            with urllib.request.urlopen(req, timeout=5):
                entry["logo"] = logo_url
                break
        except Exception:
            continue

    return entry


def write_readme(source):
    lines = []
    lines.append("<p align=\"center\">\n")
    lines.append("  <h1 align=\"center\">🎯 KK 个人插件源</h1>\n")
    lines.append("</p>\n\n")

    lines.append("## 📡 订阅地址\n\n")
    lines.append("在 AstrBot **WebUI → 「插件源管理」** 中添加以下地址：\n\n")
    lines.append("```\n")
    lines.append("https://raw.githubusercontent.com/konley/astrbot-kkplugin-source/main/plugin_source.json\n")
    lines.append("```\n\n")
    lines.append("添加后即可在 **「插件市场」** 中浏览和安装本源的插件。\n\n")
    lines.append("---\n\n")
    lines.append(f"🔄 共 **{len(source)}** 个插件 | 更新于 **{date.today()}**\n\n")

    lines.append("| 图标 | 插件信息 |\n")
    lines.append("|:----:|:--------|\n")

    for name in sorted(source.keys()):
        e = source[name]
        display = e.get("display_name", "") or name
        desc = e.get("desc", "")
        author = e.get("author", "")
        version = e.get("version", "")
        repo = e.get("repo", "")
        platforms = e.get("support_platforms", [])
        logo = e.get("logo", "")

        # Icon column
        if logo:
            icon = f"<img src=\"{logo}\" width=\"64\">"
        else:
            icon = "📦"

        # Info column: name + desc + badges + repo
        info = f"<b>{display}</b>"
        if desc:
            info += f"<br>{desc}"
        meta_parts = []
        if author:
            meta_parts.append(f"👤 {author}")
        if version:
            meta_parts.append(f"📦 {version}")
        if platforms:
            meta_parts.append(f"🔧 {', '.join(platforms)}")
        if meta_parts:
            info += f"<br><br><code>{'</code> <code>'.join(meta_parts)}</code>"
        if repo:
            info += f"<br><br><a href=\"{repo}\">📂 GitHub 仓库 →</a>"

        lines.append(f"| {icon} | {info} |\n")

    with open(README_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _ensure_gitignore():
    gitignore = os.path.join(BASE_DIR, ".gitignore")
    content = ""
    if os.path.exists(gitignore):
        with open(gitignore, encoding="utf-8") as f:
            content = f.read()
    if ".config" not in content:
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write("\n.config\n")


def _ensure_config():
    if os.path.exists(CONFIG_FILE):
        return
    _ensure_gitignore()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write("# GitHub Personal Access Token（可选，遇限速时填写）\n")
        f.write("# https://github.com/settings/tokens (public_repo)\n")
        f.write('GITHUB_TOKEN="your_token_here"\n')


def main():
    _ensure_config()
    _ensure_gitignore()

    print("=== 扫描插件目录 ===")
    dirs = scan_plugin_dirs()
    print(f"找到 {len(dirs)} 个插件目录: {', '.join(dirs)}\n")

    print("=== 读取 metadata.yaml ===")
    metas = []
    for d in dirs:
        meta = read_metadata(d)
        if meta:
            metas.append(meta)
            print(f"  {d}: {meta.get('display_name', meta.get('plugin_name', '?'))}")
        else:
            print(f"  {d}: (无 metadata.yaml，跳过)")

    print(f"\n有效插件: {len(metas)}\n")

    token_info = "已使用 Token" if get_token() else "未使用 Token（未认证，60次/小时）"
    print(f"=== 并行抓取 GitHub API (token状态: {token_info}) ===")

    source = {}
    rate_limited = False
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(enrich_plugin, meta, rate_limited): meta["dir"]
                   for meta in metas}
        for future in as_completed(futures):
            dir_name = futures[future]
            try:
                entry = future.result()
                source[dir_name] = entry
            except Exception as e:
                source[dir_name] = {"display_name": dir_name, "desc": "", "author": "", "repo": ""}
            done += 1

    print(f"完成: {len(source)} 个插件\n")

    print("=== 生成文件 ===")
    with open(SOURCE_FILE, "w", encoding="utf-8") as f:
        json.dump(source, f, ensure_ascii=False, indent=2)
    print(f"  plugin_source.json ({len(source)} 个插件)")

    write_readme(source)
    print(f"  README.md")

    print("\n订阅地址: https://raw.githubusercontent.com/konley/astrbot-kkplugin-source/main/plugin_source.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KK 插件源生成脚本")
    parser.add_argument("--token", help="GitHub Token（可选，遇限速时使用）")
    args = parser.parse_args()
    _CLI_TOKEN = args.token or ""
    main()
