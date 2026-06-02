"""
B站 TV 扫码登录 / token 续期工具 —— 多账号友好

账号统一存在 tokens.json（唯一来源），users.yaml 只放全局配置（CRON / CD / 推送等）。
tokens.json 每个账号：uid / name / access_key / refresh_token / expires_at / white_uid / banned_uid。

登录（默认）：扫码后把账号写进 tokens.json（按 uid 去重，保留已有的 white/banned）。
    python3 login.py

迁移：把旧 users.yaml 的 USERS 账号一次性搬进 tokens.json（补 uid/name；refresh_token 需重扫才有）。
    python3 login.py --migrate

续期：对临近到期的 token 续期（daemon 已每天自动调用，一般无需手动）。
    python3 login.py --refresh            # 默认 30 天内到期的续期
    python3 login.py --refresh --days 60

一个 docker 跑全部（daemon 自动续期；加账号进正在跑的容器扫码）：
  1) daemon 挂上 tokens.json（先 touch 一个空文件）：
       -v /你的/tokens.json:/app/fansMedalHelper/tokens.json
     users.yaml 仍挂着放全局配置。
  2) 加账号： docker exec -it <容器名> python3 login.py
  3) 续期： daemon 每天自动做，无需操作。
"""
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from src.api import BiliApi, Crypto, SingableDict  # noqa: E402

PASSPORT = "https://passport.bilibili.com"
_HERE = os.path.dirname(os.path.abspath(__file__))
USERS_YAML = os.path.join(_HERE, "users.yaml")
TOKENS_JSON = os.environ.get("TOKENS_FILE") or os.path.join(_HERE, "tokens.json")


def _post(path: str, params: dict) -> dict:
    body = urlencode(SingableDict(params).signed).encode()
    headers = {**BiliApi.headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = Request(f"{PASSPORT}{path}", data=body, headers=headers)
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def get_qrcode() -> tuple:
    """获取登录二维码链接与 auth_code"""
    params = {"appkey": Crypto.APPKEY, "local_id": "0", "ts": int(time.time())}
    data = _post("/x/passport-tv-login/qrcode/auth_code", params)
    if data["code"] != 0:
        raise RuntimeError(f"获取二维码失败: {data}")
    return data["data"]["url"], data["data"]["auth_code"]


def poll(auth_code: str):
    """轮询扫码结果，成功返回 token 数据，二维码失效返回 None"""
    while True:
        params = {
            "appkey": Crypto.APPKEY,
            "auth_code": auth_code,
            "local_id": "0",
            "ts": int(time.time()),
        }
        data = _post("/x/passport-tv-login/qrcode/poll", params)
        code = data["code"]
        if code == 0:
            print()
            return data["data"]
        elif code == 86038:  # 二维码已失效
            print("\n二维码已失效，重新获取...")
            return None
        elif code == 86090:  # 已扫码，待手机确认
            print("已扫码，请在手机上点击确认...", end="\r")
        # 86039 / 其它：尚未扫码，继续轮询
        time.sleep(3)


def show_qrcode(url: str):
    print("\n请用 B站 App 扫码登录（或把下面的链接复制到手机 B站 打开）：")
    print(f"  {url}\n")
    try:
        import qrcode
    except ImportError:
        try:
            import subprocess

            subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "qrcode"])
            import qrcode
        except Exception:
            print("（未能渲染终端二维码，请直接使用上面的链接登录）\n")
            return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)


def get_account(access_key: str) -> tuple:
    """用 access_key 拉取昵称与 mid，顺便确认 key 有效"""
    params = {
        "access_key": access_key,
        "actionKey": "appkey",
        "appkey": Crypto.APPKEY,
        "ts": int(time.time()),
    }
    url = "https://app.bilibili.com/x/v2/account/mine?" + urlencode(SingableDict(params).signed)
    req = Request(url, headers=BiliApi.headers)
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    info = data.get("data") or {}
    return info.get("name", ""), info.get("mid", 0)


def load_tokens() -> dict:
    if not os.path.exists(TOKENS_JSON):
        return {}
    try:
        with open(TOKENS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_tokens(tokens: dict):
    with open(TOKENS_JSON, "w", encoding="utf-8") as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)


def store_token(uid: int, name: str, access_key: str, refresh_token: str, expires_in: int):
    """把账号写进 tokens.json（按 uid 索引，保留已有的 white/banned 设置）"""
    if not uid or not refresh_token:
        return
    tokens = load_tokens()
    prev = tokens.get(str(uid), {})
    tokens[str(uid)] = {
        "uid": uid,
        "name": name,
        "access_key": access_key,
        "refresh_token": refresh_token,
        "expires_at": int(time.time()) + int(expires_in),
        "white_uid": prev.get("white_uid", "0"),
        "banned_uid": prev.get("banned_uid", "0"),
    }
    save_tokens(tokens)


def _refresh_one(entry: dict) -> tuple:
    """调刷新接口，返回 (new_access_key, new_refresh_token, expires_in, mid)"""
    params = {
        "access_key": entry["access_key"],
        "appkey": Crypto.APPKEY,
        "refresh_token": entry["refresh_token"],
        "ts": int(time.time()),
    }
    data = _post("/api/v2/oauth2/refresh_token", params)
    if data["code"] != 0:
        raise RuntimeError(f"code={data['code']} {data.get('message', '')}")
    ti = data["data"]["token_info"]
    return ti["access_token"], ti["refresh_token"], ti["expires_in"], ti.get("mid", entry.get("uid"))


def refresh_all(days: int = 30, quiet: bool = False):
    """对 tokens.json 中 days 天内到期的 token 续期（直接更新 tokens.json）。

    quiet=True 时只在真正续期/失败时输出（供 daemon 每日调用，避免刷屏）。
    """
    tokens = load_tokens()
    if not tokens:
        if not quiet:
            print(f"tokens.json 为空或不存在（{TOKENS_JSON}），没有可续期的 token。")
            print("提示：用 python3 login.py 扫码登录后才会生成续期信息。")
        return
    now = int(time.time())
    threshold = days * 86400
    changed = False
    for uid, e in tokens.items():
        name = e.get("name") or uid
        if not e.get("refresh_token"):
            if not quiet:
                print(f"[{name}] 无 refresh_token（迁移来的账号需重新扫码），跳过")
            continue
        left = e.get("expires_at", 0) - now
        if left > threshold:
            if not quiet:
                print(f"[{name}] 还有 {left // 86400} 天到期，跳过")
            continue
        try:
            new_ak, new_rt, exp_in, _ = _refresh_one(e)
        except Exception as ex:
            print(f"[{name}] 续期失败：{ex}")
            continue
        e.update(access_key=new_ak, refresh_token=new_rt, expires_at=now + int(exp_in))
        until = datetime.fromtimestamp(e["expires_at"]).strftime("%Y-%m-%d")
        print(f"[{name}] 续期成功，新到期 {until}")
        changed = True
    if changed:
        save_tokens(tokens)
        if not quiet:
            print("\n完成。")
    elif not quiet:
        print("\n没有需要续期的 token。")


def migrate_from_yaml():
    """把旧 users.yaml 的 USERS 账号一次性搬进 tokens.json"""
    if not os.path.exists(USERS_YAML):
        print(f"未找到 {USERS_YAML}，无需迁移。")
        return
    import yaml

    with open(USERS_YAML, "r", encoding="utf-8") as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader) or {}
    legacy = cfg.get("USERS") or []
    tokens = load_tokens()
    existing_keys = {e.get("access_key") for e in tokens.values()}
    migrated = 0
    for u in legacy:
        ak = u.get("access_key")
        if not ak or ak in existing_keys:
            continue
        try:
            name, mid = get_account(ak)
        except Exception as ex:
            print(f"跳过一个账号（拉取信息失败：{ex}）")
            continue
        if not mid:
            print("跳过一个账号（access_key 可能已失效）")
            continue
        tokens[str(mid)] = {
            "uid": mid,
            "name": name,
            "access_key": ak,
            "refresh_token": "",
            "expires_at": 0,
            "white_uid": str(u.get("white_uid", "0")),
            "banned_uid": str(u.get("banned_uid", "0")),
        }
        existing_keys.add(ak)
        migrated += 1
        print(f"已迁移 [{name}] (uid={mid})")
    if migrated:
        save_tokens(tokens)
    print(f"\n迁移完成，共 {migrated} 个账号。")
    if migrated:
        print("注意：迁移来的账号还没有 refresh_token，无法自动续期；")
        print("      想要自动续期，用 python3 login.py 重新扫码一次即可。")


def login_loop():
    while True:
        try:
            url, auth_code = get_qrcode()
        except Exception as e:
            print(f"获取二维码失败：{e}")
            return
        show_qrcode(url)
        result = poll(auth_code)
        if result is None:
            continue  # 二维码失效，重新生成
        access_key = result["access_token"]
        uname, mid = get_account(access_key)
        mid = mid or result.get("mid", 0)
        print(f"登录成功：{uname} (mid={mid})")
        store_token(mid, uname, access_key, result.get("refresh_token", ""), result.get("expires_in", 0))
        print("已记入 tokens.json")
        if input("\n继续添加下一个账号？(y/N) ").strip().lower() != "y":
            break
    print("完成。")


def main():
    if "--migrate" in sys.argv:
        migrate_from_yaml()
    elif "--refresh" in sys.argv:
        days = 30
        if "--days" in sys.argv:
            try:
                days = int(sys.argv[sys.argv.index("--days") + 1])
            except (ValueError, IndexError):
                print("--days 后面要跟天数，例如 --days 60")
                return
        refresh_all(days)
    else:
        login_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消")
