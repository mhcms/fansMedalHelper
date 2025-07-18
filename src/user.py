from aiohttp import ClientSession, ClientTimeout
import sys
import os
import asyncio
import uuid
import time
from loguru import logger
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class BiliUser:
    def __init__(self, access_token: str, whiteUIDs: str = '', bannedUIDs: str = '', config: dict = {}):
        from .api import BiliApi

        self.mid, self.name = 0, ""
        self.access_key = access_token  # 登录凭证
        try:
            self.whiteList = list(map(lambda x: int(x if x else 0), str(whiteUIDs).split(',')))  # 白名单UID
            self.bannedList = list(map(lambda x: int(x if x else 0), str(bannedUIDs).split(',')))  # 黑名单
        except ValueError:
            raise ValueError("白名单或黑名单格式错误")
        self.config = config
        self.medals = []  # 用户所有勋章
        self.medalsNeedDo = []  # 用户所有勋章，等级小于20的 未满1500的

        self.session = ClientSession(timeout=ClientTimeout(total=3), trust_env = True)
        self.api = BiliApi(self, self.session)

        self.retryTimes = 0  # 点赞任务重试次数
        self.maxRetryTimes = 10  # 最大重试次数
        self.message = []
        self.errmsg = ["错误日志："]
        self.uuids = [str(uuid.uuid4()) for _ in range(2)]

    async def loginVerify(self) -> bool:
        """
        登录验证
        """
        loginInfo = await self.api.loginVerift()
        self.mid, self.name = loginInfo['mid'], loginInfo['name']
        self.log = logger.bind(user=self.name)
        if loginInfo['mid'] == 0:
            self.isLogin = False
            return False
        userInfo = await self.api.getUserInfo()
        if userInfo['medal']:
            medalInfo = await self.api.getMedalsInfoByUid(userInfo['medal']['target_id'])
            if medalInfo['has_fans_medal']:
                self.initialMedal = medalInfo['my_fans_medal']
        self.log.log("SUCCESS", str(loginInfo['mid']) + " 登录成功")
        self.isLogin = True
        return True

    async def getMedals(self):
        """
        获取用户勋章
        """
        self.medals.clear()
        self.medalsNeedDo.clear()
        async for medal in self.api.getFansMedalandRoomID():
            if self.whiteList == [0]:
                if medal['medal']['target_id'] in self.bannedList:
                    self.log.warning(f"{medal['anchor_info']['nick_name']} 在黑名单中，已过滤")
                    continue
                self.medals.append(medal) if medal['room_info']['room_id'] != 0 else ...
            else:
                if medal['medal']['target_id'] in self.whiteList:
                    self.medals.append(medal) if medal['room_info']['room_id'] != 0 else ...
                    self.log.success(f"{medal['anchor_info']['nick_name']} 在白名单中，加入任务")
        [
            self.medalsNeedDo.append(medal)
            for medal in self.medals
            if medal['medal']['level'] < 20 and medal['medal']['today_feed'] < 1500
        ]

    async def like_v3(self, failedMedals: list = []):
        if self.config['LIKE_CD'] == 0:
            self.log.log("INFO", "点赞任务已关闭")
            return
        try:
            if not failedMedals:
                failedMedals = self.medals
            
            if not self.config['ASYNC']:
                self.log.log("INFO", "同步点赞任务开始....")
                for index, medal in enumerate(failedMedals):
                    for i in range(30):
                        await self.api.likeInteractV3(medal['room_info']['room_id'], medal['medal']['target_id'], self.mid)
                        if self.config['LIKE_CD'] > 0:
                            await asyncio.sleep(self.config['LIKE_CD'])
                    self.log.log(
                        "SUCCESS",
                        f"{medal['anchor_info']['nick_name']} 点赞30次成功 {index+1}/{len(failedMedals)}",
                    )
            else:
                self.log.log("INFO", "异步点赞任务开始....")
                # 异步模式：为每个房间创建独立的任务
                async def like_medal_async(medal, medal_index):
                    for i in range(30):
                        await self.api.likeInteractV3(medal['room_info']['room_id'], medal['medal']['target_id'], self.mid)
                        if self.config['LIKE_CD'] > 0:
                            await asyncio.sleep(self.config['LIKE_CD'])
                    self.log.log("SUCCESS", f"{medal['anchor_info']['nick_name']} 异步点赞30次成功 ({medal_index+1}/{len(failedMedals)})")
                
                # 创建所有点赞任务并并发执行
                like_tasks = [
                    like_medal_async(medal, index) 
                    for index, medal in enumerate(failedMedals)
                ]
                await asyncio.gather(*like_tasks)
            
            await asyncio.sleep(2)  # 短暂休息
            self.log.log("SUCCESS", "点赞任务完成")
            
        except Exception as e:
            self.log.exception("点赞任务异常")
            self.errmsg.append(f"【{self.name}】 点赞任务异常,请检查日志")

    async def sendDanmaku(self):
        """
        每日弹幕打卡
        """
        if not self.config['DANMAKU_CD']:
            self.log.log("INFO", "弹幕任务关闭")
            return
        
        # 应用过滤条件，只处理需要发送弹幕的勋章
        filtered_medals = [
            medal for medal in self.medals
            if self._shouldSendDanmaku(medal)
        ]
        
        total_medals = len(self.medals)
        filtered_medals_length = len(filtered_medals)
        
        if self.config['ASYNC']:
            self.log.log("INFO", f"异步弹幕打卡任务开始....共{filtered_medals_length}个房间")
            await self._sendDanmakuAsync(filtered_medals)
        else:
            self.log.log("INFO", f"同步弹幕打卡任务开始....预计 {filtered_medals_length * self.config['DANMAKU_CD'] * self.config['DANMAKU_NUM']} 秒完成")
            await self._sendDanmakuSync(filtered_medals)
        
        if hasattr(self, 'initialMedal'):
            (await self.api.wearMedal(self.initialMedal['medal_id'])) if self.config['WEARMEDAL'] else ...
        self.log.log("SUCCESS", "弹幕打卡任务完成")
        self.message.append(f"【{self.name}】 弹幕打卡任务完成 {filtered_medals_length}/{total_medals}")

    def _shouldSendDanmaku(self, medal):
        """判断是否应该发送弹幕"""
        if self.config['DANMAKU_CHECK_LIGHT'] and medal['medal']['is_lighted'] == 1:
            return False
        if not self.config['DANMAKU_CHECK_LEVEL'] and medal['medal']['level'] > 20:
            return False
        return True

    async def _sendDanmakuSync(self, filtered_medals):
        """同步弹幕发送"""
        successnum = 0
        for n, medal in enumerate(filtered_medals, 1):
            (await self.api.wearMedal(medal['medal']['medal_id'])) if self.config['WEARMEDAL'] else ...
            
            for i in range(self.config['DANMAKU_NUM']):
                try:
                    danmaku = await self.api.sendDanmaku(medal['room_info']['room_id'])
                    self.log.log(
                        "INFO",
                        f"{medal['anchor_info']['nick_name']} 房间弹幕打卡({i + 1}/{self.config['DANMAKU_NUM']})成功: {danmaku} ({n}/{len(filtered_medals)})",
                    )
                except Exception as e:
                    self.log.log("ERROR", f"{medal['anchor_info']['nick_name']} 房间弹幕打卡({i + 1}/{self.config['DANMAKU_NUM']})失败: {e}")
                    self.errmsg.append(f"【{self.name}】 {medal['anchor_info']['nick_name']} 房间弹幕打卡失败: {str(e)}")
                finally:
                    if i < self.config['DANMAKU_NUM'] - 1:  # 最后一次不需要等待
                        await asyncio.sleep(self.config['DANMAKU_CD'])
            successnum += 1

    async def _sendDanmakuAsync(self, filtered_medals):
        """异步弹幕发送"""
        async def send_danmaku_for_medal(medal, medal_index):
            (await self.api.wearMedal(medal['medal']['medal_id'])) if self.config['WEARMEDAL'] else ...
            
            success_count = 0
            for i in range(self.config['DANMAKU_NUM']):
                try:
                    danmaku = await self.api.sendDanmaku(medal['room_info']['room_id'])
                    success_count += 1
                    self.log.log(
                        "INFO",
                        f"{medal['anchor_info']['nick_name']} 异步弹幕打卡({i + 1}/{self.config['DANMAKU_NUM']})成功: {danmaku} ({medal_index+1}/{len(filtered_medals)})",
                    )
                except Exception as e:
                    self.log.log("ERROR", f"{medal['anchor_info']['nick_name']} 异步弹幕打卡({i + 1}/{self.config['DANMAKU_NUM']})失败: {e}")
                    self.errmsg.append(f"【{self.name}】 {medal['anchor_info']['nick_name']} 异步弹幕打卡失败: {str(e)}")
                
                if i < self.config['DANMAKU_NUM'] - 1:  # 最后一次不需要等待
                    await asyncio.sleep(self.config['DANMAKU_CD'])
            return success_count > 0
        
        # 创建所有弹幕任务并并发执行
        danmaku_tasks = [
            send_danmaku_for_medal(medal, index)
            for index, medal in enumerate(filtered_medals)
        ]
        await asyncio.gather(*danmaku_tasks)

    async def init(self):
        if not await self.loginVerify():
            self.log.log("ERROR", "登录失败 可能是 access_key 过期 , 请重新获取")
            self.errmsg.append("登录失败 可能是 access_key 过期 , 请重新获取")
            await self.session.close()
        else:
            await self.getMedals()

    async def start(self):
        if not self.isLogin:
            return
        
        self.log.log("INFO", f"开始执行任务 - 异步模式: {'开启' if self.config['ASYNC'] else '关闭'}")
        
        # 观看直播任务必须单独执行，因为需要维护连续的观看状态
        # 其他任务可以根据配置决定是否并发执行
        
        if self.medalsNeedDo:
            self.log.log("INFO", f"共有 {len(self.medalsNeedDo)} 个牌子未满 1500 亲密度")
            
            if self.config['ASYNC']:
                # 异步模式：点赞任务独立执行，观看直播必须顺序执行
                self.log.log("INFO", "异步模式：点赞任务将与其他任务并发，观看直播顺序执行")
                
                # 创建可以并发的任务列表（不包括观看直播）
                concurrent_tasks = []
                concurrent_tasks.append(self.like_v3())
                concurrent_tasks.append(self.sendDanmaku())
                concurrent_tasks.append(self.signInGroups())
                
                # 并发执行点赞、弹幕、应援团签到，然后顺序执行观看直播
                await asyncio.gather(*concurrent_tasks)
                await self.watchinglive()  # 观看直播必须单独执行
                
            else:
                # 同步模式：所有任务按顺序执行
                self.log.log("INFO", "同步模式：所有任务将按顺序执行")
                
                # 按正确顺序执行各个任务：点赞 -> 弹幕 -> 应援团签到 -> 观看直播
                await self.like_v3()
                await self.sendDanmaku()
                await self.signInGroups()
                await self.watchinglive()  # 观看直播放在最后，因为它耗时最长
        else:
            self.log.log("INFO", "所有牌子已满 1500 亲密度，跳过点赞和观看直播任务")
            
            if self.config['ASYNC']:
                # 异步执行剩余任务
                remaining_tasks = [self.sendDanmaku(), self.signInGroups()]
                await asyncio.gather(*remaining_tasks)
            else:
                # 顺序执行剩余任务
                await self.sendDanmaku()
                await self.signInGroups()
        
        self.log.log("SUCCESS", "所有任务执行完成")

    async def sendmsg(self):
        if not self.isLogin:
            await self.session.close()
            return self.message + self.errmsg
        await self.getMedals()
        nameList1, nameList2, nameList3, nameList4 = [], [], [], []
        for medal in self.medals:
            if medal['medal']['level'] >= 20:
                continue
            today_feed = medal['medal']['today_feed']
            nick_name = medal['anchor_info']['nick_name']
            if today_feed >= 1500:
                nameList1.append(nick_name)
            elif 1200 <= today_feed < 1500:
                nameList2.append(nick_name)
            elif 300 <= today_feed < 1200:
                nameList3.append(nick_name)
            elif today_feed < 300:
                nameList4.append(nick_name)
        self.message.append(f"【{self.name}】 今日亲密度获取情况如下（20级以下）：")

        for l, n in zip(
            [nameList1, nameList2, nameList3, nameList4],
            ["【1500】", "【1200至1500】", "【300至1200】", "【300以下】"],
        ):
            if len(l) > 0:
                self.message.append(f"{n}{' '.join(l[:5])}{'等' if len(l) > 5 else ''} {len(l)}个")

        if hasattr(self, 'initialMedal'):
            initialMedalInfo = await self.api.getMedalsInfoByUid(self.initialMedal['target_id'])
            if initialMedalInfo['has_fans_medal']:
                initialMedal = initialMedalInfo['my_fans_medal']
                self.message.append(
                    f"【当前佩戴】「{initialMedal['medal_name']}」({initialMedal['target_name']}) {initialMedal['level']} 级 "
                )
                if initialMedal['level'] < 20 and initialMedal['today_feed'] != 0:
                    need = initialMedal['next_intimacy'] - initialMedal['intimacy']
                    need_days = need // 1500 + 1
                    end_date = datetime.now() + timedelta(days=need_days)
                    self.message.append(f"今日已获取亲密度 {initialMedal['today_feed']} (B站结算有延迟，请耐心等待)")
                    self.message.append(
                        f"距离下一级还需 {need} 亲密度 预计需要 {need_days} 天 ({end_date.strftime('%Y-%m-%d')},以每日 1500 亲密度计算)"
                    )
        await self.session.close()
        return self.message + self.errmsg + ['---']

    async def watchinglive(self):
        if not self.config['WATCHINGLIVE']:
            self.log.log("INFO", "每日观看直播任务关闭")
            return
        
        HEART_MAX = self.config['WATCHINGLIVE']
        self.log.log("INFO", f"每日{HEART_MAX}分钟任务开始")
        
        # 计算到第二天0点的截止时间
        now = datetime.now()
        next_midnight = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        end_time = next_midnight.timestamp()
        self.log.log("INFO", f"任务将在 {next_midnight.strftime('%Y-%m-%d %H:%M:%S')} 前结束")
        
        # 观看直播必须顺序执行，不能并发，因为B站心跳包需要维护连续的观看状态
        self.log.log("INFO", f"顺序观看直播任务开始....共{len(self.medalsNeedDo)}个房间")
        await self._watchingLiveSequential(HEART_MAX, end_time)
        
        self.log.log("SUCCESS", f"每日{HEART_MAX}分钟任务完成")

    async def _watchingLiveSequential(self, HEART_MAX, end_time):
        """顺序观看直播 - 观看直播必须一个房间一个房间进行"""
        n = 0
        for medal in self.medalsNeedDo:
            n += 1
            self.log.log("INFO", f"开始观看 {medal['anchor_info']['nick_name']} 的直播间 ({n}/{len(self.medalsNeedDo)})")
            
            for heartNum in range(1, HEART_MAX + 1):
                # 检查剩余时间是否足够完成下一次心跳包（预留90秒缓冲时间）
                current_timestamp = time.time()
                time_remaining = end_time - current_timestamp
                
                if time_remaining < 90:  # 如果剩余时间不足90秒，提前结束
                    remaining_hours = int(time_remaining // 3600)
                    remaining_minutes = int((time_remaining % 3600) // 60)
                    remaining_seconds = int(time_remaining % 60)
                    self.log.log("INFO", f"距离24点还有 {remaining_hours}时{remaining_minutes}分{remaining_seconds}秒，提前结束直播任务，等待新的一轮")
                    return
                
                await self.api.heartbeat(medal['room_info']['room_id'], medal['medal']['target_id'])
                
                if heartNum % 5 == 0:
                    # 计算剩余时间并显示
                    current_timestamp = time.time()
                    time_remaining = end_time - current_timestamp
                    remaining_hours = int(time_remaining // 3600)
                    remaining_minutes = int((time_remaining % 3600) // 60)
                    self.log.log(
                        "INFO",
                        f"{medal['anchor_info']['nick_name']} 第{heartNum}次心跳包已发送（{n}/{len(self.medalsNeedDo)}）- 距离24点还有{remaining_hours}时{remaining_minutes}分",
                    )
                await asyncio.sleep(60)
            
            self.log.log("SUCCESS", f"{medal['anchor_info']['nick_name']} 观看完成 ({n}/{len(self.medalsNeedDo)})")

    async def signInGroups(self):
        # 检查SIGNINGROUP配置，如果用户明确设置为字符串"0"或布尔False，则跳过签到
        if (isinstance(self.config.get('SIGNINGROUP'), str) and self.config['SIGNINGROUP'] == "0") or \
           self.config.get('SIGNINGROUP') is False:
            self.log.log("INFO", "应援团签到任务已关闭")
            return
        
        self.log.log("INFO", "应援团签到任务开始")
        try:
            # 收集所有应援团信息
            groups = []
            async for group in self.api.getGroups():
                if group['owner_uid'] != self.mid:  # 排除自己的应援团
                    groups.append(group)
            
            if not groups:
                self.log.log("WARNING", "没有加入应援团")
                return
            
            if self.config['ASYNC']:
                self.log.log("INFO", f"异步应援团签到开始....共{len(groups)}个应援团")
                success_count = await self._signInGroupsAsync(groups)
            else:
                self.log.log("INFO", f"同步应援团签到开始....共{len(groups)}个应援团")
                success_count = await self._signInGroupsSync(groups)
            
            if success_count > 0:
                self.log.log("SUCCESS", f"应援团签到任务完成 {success_count}/{len(groups)}")
                self.message.append(f" 应援团签到任务完成 {success_count}/{len(groups)}")
            
        except Exception as e:
            self.log.exception(e)
            self.log.log("ERROR", "应援团签到任务失败: " + str(e))
            self.errmsg.append("应援团签到任务失败: " + str(e))

    async def _signInGroupsSync(self, groups):
        """同步应援团签到"""
        success_count = 0
        for group in groups:
            try:
                await self.api.signInGroups(group['group_id'], group['owner_uid'])
                self.log.log("DEBUG", f"{group['group_name']} 签到成功")
                success_count += 1
                if self.config['SIGNINGROUP'] > 0:
                    await asyncio.sleep(self.config['SIGNINGROUP'])
            except Exception as e:
                self.log.log("ERROR", f"{group['group_name']} 签到失败: {e}")
                self.errmsg.append(f"应援团签到失败: {e}")
        return success_count

    async def _signInGroupsAsync(self, groups):
        """异步应援团签到"""
        async def sign_in_group_async(group):
            try:
                await self.api.signInGroups(group['group_id'], group['owner_uid'])
                self.log.log("DEBUG", f"{group['group_name']} 异步签到成功")
                return True
            except Exception as e:
                self.log.log("ERROR", f"{group['group_name']} 异步签到失败: {e}")
                self.errmsg.append(f"应援团签到失败: {e}")
                return False
        
        # 如果设置了CD时间，需要分批执行以避免过于频繁
        if self.config['SIGNINGROUP'] > 0:
            success_count = 0
            for group in groups:
                result = await sign_in_group_async(group)
                if result:
                    success_count += 1
                await asyncio.sleep(self.config['SIGNINGROUP'])
            return success_count
        else:
            # 如果SIGNINGROUP为0，可以完全并发执行
            sign_tasks = [sign_in_group_async(group) for group in groups]
            results = await asyncio.gather(*sign_tasks)
            return sum(results)
