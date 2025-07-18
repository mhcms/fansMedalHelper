import json
import os
import sys
from loguru import logger
import warnings
import asyncio
import aiohttp
import itertools
from src import BiliUser

log_file = os.path.join(os.path.dirname(__file__), "log/fansMedalHelper_{time:YYYY-MM-DD}.log")
log_format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"

logger.remove()
logger.add(
    sys.stdout,
    format=log_format,
    backtrace=True,
    diagnose=True,
    level="INFO"
)
log = logger.bind(user="B站粉丝牌助手")
__VERSION__ = "0.3.8"

warnings.filterwarnings(
    "ignore",
    message="The localize method is no longer necessary, as this time zone supports the fold attribute",
)
os.chdir(os.path.dirname(os.path.abspath(__file__)).split(__file__)[0])

try:
    if os.environ.get("USERS"):
        users = json.loads(os.environ.get("USERS"))
    else:
        import yaml

        with open("users.yaml", "r", encoding="utf-8") as f:
            users = yaml.load(f, Loader=yaml.FullLoader)
    if users.get("WRITE_LOG_FILE"):
        logger.add(
            log_file if users["WRITE_LOG_FILE"] == True else users["WRITE_LOG_FILE"],
            format=log_format,
            backtrace=True,
            diagnose=True,
            rotation="00:00",
            retention="30 days",
            level="DEBUG"
        )
    assert users["ASYNC"] in [0, 1], "ASYNC参数错误"
    assert users["LIKE_CD"] >= 0, "LIKE_CD参数错误"
    # assert users['SHARE_CD'] >= 0, "SHARE_CD参数错误"
    assert users["DANMAKU_CD"] >= 0, "DANMAKU_CD参数错误"
    assert users["DANMAKU_NUM"] >= 0, "DANMAKU_NUM参数错误"
    assert users["DANMAKU_CHECK_LIGHT"] in [0, 1], "DANMAKU_CHECK_LIGHT参数错误"
    assert users["DANMAKU_CHECK_LEVEL"] in [0, 1], "DANMAKU_CHECK_LEVEL参数错误"
    assert users["WATCHINGLIVE"] >= 0, "WATCHINGLIVE参数错误"
    assert users["WEARMEDAL"] in [0, 1], "WEARMEDAL参数错误"
    config = {
        "ASYNC": users["ASYNC"],
        "LIKE_CD": users["LIKE_CD"],
        # "SHARE_CD": users['SHARE_CD'],
        "DANMAKU_CD": users["DANMAKU_CD"],
        "DANMAKU_NUM": users["DANMAKU_NUM"],
        "DANMAKU_CHECK_LIGHT": users["DANMAKU_CHECK_LIGHT"],
        "DANMAKU_CHECK_LEVEL": users["DANMAKU_CHECK_LEVEL"],
        "DANMAKU_TEXTS": users.get("DANMAKU_TEXTS", []),  # 读取自定义弹幕内容
        "WATCHINGLIVE": users["WATCHINGLIVE"],
        "WEARMEDAL": users["WEARMEDAL"],
        "SIGNINGROUP": users.get("SIGNINGROUP", 2),  # 修改默认值为2，与配置文件一致
        "PROXY": users.get("PROXY"),
    }
except Exception as e:
    log.error(f"读取配置文件失败,请检查配置文件格式是否正确: {e}")
    exit(1)


@log.catch
async def main():
    messageList = []
    session = aiohttp.ClientSession(trust_env=True)
    try:
        log.warning("当前版本为: " + __VERSION__)
        resp = await (
            await session.get(
                "http://version.fansmedalhelper.1961584514352337.cn-hangzhou.fc.devsapp.net/"
            )
        ).json()
        if resp["version"] != __VERSION__:
            log.warning("新版本为: " + resp["version"] + ",请更新")
            log.warning("更新内容: " + resp["changelog"])
            messageList.append(f"当前版本: {__VERSION__} ,最新版本: {resp['version']}")
            messageList.append(f"更新内容: {resp['changelog']} ")
        if resp["notice"]:
            log.warning("公告: " + resp["notice"])
            messageList.append(f"公告: {resp['notice']}")
    except Exception as ex:
        messageList.append(f"检查版本失败，{ex}")
        log.warning(f"检查版本失败，{ex}")
    initTasks = []
    startTasks = []
    catchMsg = []
    for user in users["USERS"]:
        if user["access_key"]:
            biliUser = BiliUser(
                user["access_key"],
                user.get("white_uid", ""),
                user.get("banned_uid", ""),
                config,
            )
            initTasks.append(biliUser.init())
            startTasks.append(biliUser.start())
            catchMsg.append(biliUser.sendmsg())
    try:
        await asyncio.gather(*initTasks)
        await asyncio.gather(*startTasks)
    except Exception as e:
        log.exception(e)
        # messageList = messageList + list(itertools.chain.from_iterable(await asyncio.gather(*catchMsg)))
        messageList.append(f"任务执行失败: {e}")
    finally:
        messageList = messageList + list(
            itertools.chain.from_iterable(await asyncio.gather(*catchMsg))
        )
    [log.info(message) for message in messageList]
    if users.get("SENDKEY", ""):
        await push_message(session, users["SENDKEY"], "  \n".join(messageList))
    await session.close()
    if users.get("MOREPUSH", ""):
        from onepush import notify

        notifier = users["MOREPUSH"]["notifier"]
        params = users["MOREPUSH"]["params"]
        await notify(
            notifier,
            title=f"【B站粉丝牌助手推送】",
            content="  \n".join(messageList),
            **params,
            proxy=config.get("PROXY"),
        )
        log.info(f"{notifier} 已推送")


def should_run_immediately(cron_expression):
    """
    判断是否应该立即执行任务
    如果当前时间已经错过了今天的定时执行时间，返回True
    """
    if not cron_expression:
        return False
    
    try:
        from apscheduler.triggers.cron import CronTrigger
        import datetime
        
        # 创建cron触发器
        trigger = CronTrigger.from_crontab(cron_expression, timezone="Asia/Shanghai")
        
        # 获取当前时间
        now = datetime.datetime.now(tz=trigger.timezone)
        
        # 获取今天0点
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 获取今天的执行时间
        today_run_time = trigger.get_next_fire_time(None, today_start)
        
        # 如果今天的执行时间存在且是今天
        if today_run_time and today_run_time.date() == now.date():
            # 检查当前时间是否已经超过了今天的执行时间
            if now > today_run_time:
                # 如果错过了今天的执行时间，都应该补执行（一天内有效）
                time_diff = (now - today_run_time).total_seconds() / 60  # 转换为分钟
                log.info(f"今天的执行时间是 {today_run_time.strftime('%H:%M:%S')}，当前时间 {now.strftime('%H:%M:%S')}，错过了 {time_diff:.1f} 分钟")
                return True
            else:
                # 今天的执行时间还没到
                time_remaining = (today_run_time - now).total_seconds() / 60
                log.info(f"今天的执行时间 {today_run_time.strftime('%H:%M:%S')} 还有 {time_remaining:.1f} 分钟到达")
                return False
        else:
            # 今天没有执行时间，等待下次
            log.info("今天没有定时执行时间，等待下次执行")
            return False
                
    except Exception as e:
        log.warning(f"检查定时执行时间失败: {e}")
    
    return False


def run(*args, **kwargs):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
    log.info("任务结束，等待下一次执行。")


async def push_message(session, sendkey, message):
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = {"title": f"【B站粉丝牌助手推送】", "desp": message}
    await session.post(url, data=data)
    log.info("Server酱已推送")


if __name__ == "__main__":
    cron = users.get("CRON", None)
    smart_start = users.get("SMART_START", True)

    # 智能启动逻辑
    should_run_now = False
    
    if smart_start == "always":
        log.info("配置为每次启动都执行，立即执行任务。")
        should_run_now = True
    elif smart_start and cron:
        if should_run_immediately(cron):
            log.info("检测到已错过今天的定时执行时间，智能启动：立即执行任务。")
            should_run_now = True
        else:
            from apscheduler.triggers.cron import CronTrigger
            import datetime
            
            trigger = CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
            now = datetime.datetime.now(tz=trigger.timezone)
            
            # 先检查今天是否还有执行时间
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_run_time = trigger.get_next_fire_time(None, today_start)
            
            if today_run_time and today_run_time.date() == now.date() and now < today_run_time:
                # 今天还有执行时间且尚未到达
                time_remaining = (today_run_time - now).total_seconds() / 60
                log.info(f"智能启动检测：今天的定时执行时间 {today_run_time.strftime('%H:%M:%S')} 还有 {time_remaining:.1f} 分钟到达")
            else:
                # 今天没有执行时间或已经过去，显示下次执行时间
                next_run = trigger.get_next_fire_time(None, now)
                if next_run:
                    log.info(f"智能启动检测：下次执行时间：{next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    log.info("智能启动检测：等待定时器执行。")
    elif smart_start:
        log.info("智能启动模式开启，但未配置CRON，立即执行任务。")
        should_run_now = True
    elif cron:
        log.info("智能启动模式关闭，严格按照定时器执行。")
    else:
        log.info("未配置CRON且智能启动关闭，单次执行模式。")
        should_run_now = True

    if should_run_now:
        run()

    if cron:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger

        log.info(f"启动内置定时器 [{cron}]，进入守护模式...")
        schedulers = BlockingScheduler(timezone="Asia/Shanghai")
        schedulers.add_job(run, CronTrigger.from_crontab(cron), misfire_grace_time=3600)
        schedulers.start()
    elif "--auto" in sys.argv:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        import datetime

        log.info("使用自动守护模式，每天0点运行一次。")
        scheduler = BlockingScheduler(timezone="Asia/Shanghai")
        scheduler.add_job(
            run,
            CronTrigger(hour=0, minute=0),
            next_run_time=datetime.datetime.now(),
            misfire_grace_time=3600,
        )
        scheduler.start()
    else:
        if not should_run_now:
            log.info("单次执行完成，程序结束。")
        else:
            log.info("任务执行完成，程序结束。")
