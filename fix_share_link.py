"""
create_share_link monkey patch:
修复 fs_files 列目录被 405 风控截断时轮询耗尽返回 None，
导致 pipeline 报「创建分享失败：未知响应」的问题。

新增完成判定：深度规模匹配(stats_match) + 体积停滞 → 直接用任务子目录 CID 分享。
"""
import logging
_log = logging.getLogger("pipeline")


def _apply_patch():
    try:
        from app.services.p115 import P115Service
    except Exception as e:
        _log.warning(f"fix_share_link import P115Service failed: {e}")
        return
    P115Service.create_share_link = _patched_create_share_link
    _log.info("✅ create_share_link 已补丁：深度规模匹配兜底分享")

async def _patched_create_share_link(self, save_result: dict):
        if not self.client or not save_result:
            return None

        to_cid = save_result.get("to_cid")
        names = save_result.get("names", [])
        original_total_size = save_result.get("original_total_size", 0)
        original_file_count = save_result.get("original_file_count", 0)
        original_folder_count = save_result.get("original_folder_count", 0)
        # 源分享含违规文件：转存时必有文件被 115 跳过，落地体积注定小于源声明。
        # 此时不能以体积追平基准为完成条件，改由「顶层建好 + 体积停滞」判定。
        has_violation = bool(save_result.get("have_vio", False))

        # 🔑 Bug 修复: 安全断言——禁止对根保存目录或根目录(CID=0)发起分享。
        # 当 _ensure_save_dir 缓存失效后回退到旧/错误 CID 时，to_cid 可能等于根保存目录
        # 本身，导致 _snapshot_dir_ids 枚举出整个 115-Share 目录下的所有文件并分享出去。
        if not to_cid or to_cid == 0:
            logger.error(f"❌ [安全拦截] to_cid 为空或 0，拒绝创建分享链接，避免意外分享根目录内容")
            return {
                "status": "error",
                "error_type": "invalid_cid",
                "message": "任务子目录 CID 无效 (0)，无法创建分享链接"
            }
        if self._save_dir_cid > 0 and to_cid == self._save_dir_cid:
            logger.error(
                f"❌ [安全拦截] to_cid ({to_cid}) 与根保存目录 CID 相同，"
                f"拒绝创建分享链接，避免泄露整个保存目录内容"
            )
            return {
                "status": "error",
                "error_type": "root_dir_share_blocked",
                "message": "检测到即将对保存根目录发起分享，已安全拦截。请检查账号 Session 状态。"
            }

        # 源分享规模是完成判定的硬基准，不能只依赖目标目录暂时稳定。
        logger.info(f"📊 [基准比对数据] 转存源分享规模: 大小 = {original_total_size} 字节, 文件数 = {original_file_count}, 文件夹数 = {original_folder_count}")
        if has_violation:
            logger.warning(
                "⚠️ 源分享含违规文件 (have_vio=1)，预期部分文件无法保存、体积追不平基准，"
                "将以「顶层结构匹配 + 体积停滞」判定完成"
            )

        try:
            logger.info(f"⏳ 等待文件深度属性写入子孙目录 (CID: {to_cid})...")

            new_fids = []
            stable_times = 0
            max_poll_attempts = 45 # 最多等待约 90s
            min_stable_required = 3 # 稳定不变的次数要求

            # 体积停滞兜底：顶层结构已建好但体积追不平基准（如违规文件被跳过）时，
            # 体积连续不变达到阈值即判定转存完成，避免死循环空转并触发 405。
            last_seen_size = -1
            size_stagnant_times = 0
            # 违规分享注定追不平基准，用更短的停滞阈值尽快完成
            min_stagnant_required = 2 if has_violation else 3
            size_stagnant_done = False

            margin_hit_count = 0  # margin 限速不消耗轮询次数，单独计数
            max_margin_hits = 30  # margin 最多容忍 30 次（约 2.5 分钟）
            saw_405 = False
            consecutive_405 = 0
            # 目录查询连续全端点 405 达此次数后：跳过完整性校验，直接用任务子目录 CID 创建分享
            max_405_before_skip = 3
            skip_verify_share_cid = False
            # [patch] 记录是否曾观察到深度规模匹配（列目录可能被 405 截断）
            ever_stats_match = False

            for poll_attempt in range(1, max_poll_attempts + 1):
                try:
                    logger.debug(f"🔍 探测子目录属性详情 (第 {poll_attempt}/{max_poll_attempts} 次), CID: {to_cid}")
                    
                    # 1. 多端点获取实时深度体积（proapi → webapi）
                    resp = await self._fs_category_get_with_fallback(to_cid, timeout=10)
                    consecutive_405 = 0  # 任一端点成功即清零
                    
                    # 🛡️ 检测 115 限速响应 {"margin": N}，不消耗轮询次数
                    if self._is_margin_response(resp):
                        stable_times = 0
                        margin_hit_count += 1
                        margin_val = int(resp.get("margin", 5))
                        wait = max(margin_val, 3)
                        logger.warning(f"⚠️ fs_category_get 触发 115 限速 (margin={margin_val})，等待 {wait}s 后重试 (margin 第 {margin_hit_count}/{max_margin_hits} 次)")
                        if margin_hit_count >= max_margin_hits:
                            logger.warning(f"🚫 轮询阶段 margin 限速持续过久 ({margin_hit_count} 次)，转入分享排队")
                            return self._margin_limited_payload(
                                save_result,
                                "分享被限制（margin），已加入排队等待",
                                limit_reason="margin",
                            )
                        await asyncio.sleep(wait)
                        continue
                    
                    logger.info(f"📋 [深度属性 RAW API 完整响应] {resp}")
                    
                    # 2. 从原生响应中直接读取（而不是从 data 中读）
                    current_file_count = int(resp.get("count", 0) or resp.get("file_count", 0))
                    current_folder_count = int(resp.get("folder_count", 0))
                    current_total = current_file_count + current_folder_count
                    current_size = parse_size_to_bytes(resp.get("size", 0))

                    logger.info(f"⚖️ [比对进程] 当前挂载层级统计: 大小={current_size} (基准: {original_total_size} 字节), 文件数={current_file_count}, 文件夹数={current_folder_count}")

                    # 3. 结合原先的顶部结构验证，确保外层骨架确实建好了
                    current_items = await self._get_dir_items(to_cid, strict=True)
                    consecutive_405 = 0
                    orig_size = int(original_total_size or 0)
                    orig_files = int(original_file_count or 0)
                    orig_folders = int(original_folder_count or 0)
                    size_match = sizes_approximately_equal(current_size, orig_size) if orig_size > 0 else current_size > 0
                    # 源分享常缺深层级文件/文件夹计数（为 0）；此时只按体积判定，避免永远等不到匹配
                    if orig_files == 0 and orig_folders == 0:
                        stats_match = size_match
                    else:
                        stats_match = (
                            size_match
                            and current_file_count == orig_files
                            and current_folder_count == orig_folders
                        )
                    # 敏感词处理可能已修改顶层名称，因此普通分享按顶层数量精确匹配。
                    # 定时移动/复制流程会在改名前执行更严格的名称/ID匹配。
                    top_match = len(current_items) == len(names)
                    # [patch] 深度规模匹配即记录（供轮询耗尽兜底）
                    if stats_match:
                        ever_stats_match = True

                    # 体积停滞统计：顶层建好后，若体积连续不再变化，视为转存已停止增长
                    if current_size > 0 and current_size == last_seen_size:
                        size_stagnant_times += 1
                    else:
                        size_stagnant_times = 0
                    last_seen_size = current_size

                    if stats_match and top_match:
                        stable_times += 1
                        logger.info(
                            f"🔄 目标目录规模及顶层结构与源分享一致 "
                            f"(连续稳固 {stable_times}/{min_stable_required} 次)"
                        )
                        if stable_times >= min_stable_required:
                            logger.info(f"✅ 目标目录与源分享基准连续一致，确认转存完成")
                            new_fids = [item["id"] for item in current_items]
                            break
                    elif (
                        top_match
                        and current_size > 0
                        and size_stagnant_times >= min_stagnant_required
                    ):
                        # 兜底：体积追不平基准但已连续多轮停滞（常见于源含违规文件被跳过）
                        logger.warning(
                            f"⏹️ 体积连续 {size_stagnant_times} 轮停滞({current_size} 字节)"
                            f"且顶层结构匹配，判定转存完成（可能因违规文件被跳过，基准={orig_size}）"
                        )
                        new_fids = [item["id"] for item in current_items]
                        size_stagnant_done = True
                        break
                    elif (
                        stats_match
                        and current_size > 0
                        and size_stagnant_times >= min_stagnant_required
                    ):
                        # [patch] 深度规模已完全匹配基准，但顶层枚举被 405 截断（top_match=False）
                        # 直接用任务子目录 CID 创建分享，避免轮询耗尽后误报失败
                        logger.warning(
                            f"⏹️ 深度规模已匹配基准且体积连续 {size_stagnant_times} 轮停滞"
                            f"({current_size} 字节)，顶层枚举被风控截断，直接用任务子目录分享"
                        )
                        new_fids = [to_cid]
                        skip_verify_share_cid = True
                        size_stagnant_done = True
                        break
                    else:
                        logger.info(
                            "📈 目标目录尚未达到源分享基准: "
                            f"统计匹配={stats_match}, 顶层结构匹配={top_match}, "
                            f"体积停滞={size_stagnant_times}/{min_stagnant_required}, "
                            f"当前总项数={current_total}"
                        )
                        stable_times = 0
                    
                    if poll_attempt < max_poll_attempts:
                        await asyncio.sleep(2)

                except Exception as e:
                    stable_times = 0
                    if self._is_405_error(e):
                        saw_405 = True
                        consecutive_405 += 1
                        logger.warning(
                            f"🚫 目录端点全部 405 "
                            f"(连续 {consecutive_405}/{max_405_before_skip} 次, 轮询第 {poll_attempt}): {e}"
                        )
                        if consecutive_405 >= max_405_before_skip:
                            logger.warning(
                                f"⏭️ 目录校验连续 {consecutive_405} 次 405，"
                                f"跳过完整性等待，直接用任务子目录 CID={to_cid} 创建分享"
                            )
                            # 给异步转存一点收尾时间，再分享整个任务目录
                            await asyncio.sleep(15)
                            new_fids = [to_cid]
                            skip_verify_share_cid = True
                            break
                        if poll_attempt < max_poll_attempts:
                            await asyncio.sleep(5)
                        continue
                    logger.error(f"⚠️ 检索目录内容或属性失败 (第 {poll_attempt} 次): {e}", exc_info=True)
                    if poll_attempt < max_poll_attempts:
                        await asyncio.sleep(5)

            if not new_fids:
                if saw_405:
                    # 轮询耗尽仍全是 405：同样跳过校验，尝试直接分享任务目录
                    logger.warning(
                        f"⏭️ 子目录 {to_cid} 轮询结束仍无法列目录(405)，"
                        f"跳过校验，直接用任务子目录创建分享"
                    )
                    await asyncio.sleep(10)
                    new_fids = [to_cid]
                    skip_verify_share_cid = True
                elif ever_stats_match:
                    # [patch] 轮询耗尽但深度规模曾匹配基准：直接用任务子目录分享
                    logger.warning(
                        f"⏭️ 子目录 {to_cid} 轮询耗尽但深度规模曾匹配基准，"
                        f"跳过完整性校验，直接用任务子目录创建分享"
                    )
                    await asyncio.sleep(10)
                    new_fids = [to_cid]
                    skip_verify_share_cid = True
                else:
                    logger.warning(f"⚠️ 子目录 {to_cid} 中最终未检测到任何文件，可能 115 处理延迟或转存失败")
                    return None

            if skip_verify_share_cid:
                logger.info(f"🚀 [405跳过校验] 即将分享任务子目录 CID={to_cid}")

            # 7. Create new share with retry mechanism and split if > 10,000 files
            share_links = []
            fids_str_list = [str(fid) for fid in new_fids]
            max_share_retries = 3
            
            # Split fids into batches of 10,000 to respect 115 limits
            for batch_idx, i in enumerate(range(0, len(fids_str_list), 10000), 1):
                batch_fids = fids_str_list[i:i+10000]
                batch_share_code = None
                batch_receive_code = None
                
                for retry_attempt in range(1, max_share_retries + 1):
                    try:
                        logger.info(f"📤 正在创建分享链接 (分卷 {batch_idx}, 尝试 {retry_attempt}/{max_share_retries})...")
                        send_resp = await self._api_call_with_timeout(
                            self.client.share_send_app, ",".join(batch_fids), async_=True,
                            timeout=API_TIMEOUT, max_retries=1, label=f"share_send_batch_{batch_idx}",
                            **self._get_ios_ua_kwargs()
                        )
                        
                        # 主动检测 115 非标限速响应（仅含 {"margin": N}，无 state/data）
                        if self._is_margin_response(send_resp):
                            margin_val = send_resp.get("margin", 10)
                            wait = max(int(margin_val), 5)
                            logger.warning(f"⚠️ 115 分享接口触发限速 (margin={margin_val})，等待 {wait} 秒后重试... (尝试 {retry_attempt}/{max_share_retries})")
                            if retry_attempt < max_share_retries:
                                await asyncio.sleep(wait)
                                continue
                            else:
                                # 3 次都 margin，返回 margin_limited 状态进入排队
                                logger.warning(f"🚫 分享创建阶段 margin 限速 {max_share_retries} 次，转入分享排队")
                                return self._margin_limited_payload(
                                    save_result,
                                    "分享被限制（margin），已加入排队等待",
                                    limit_reason="margin",
                                )
                        
                        check_response(send_resp)
                        logger.debug(f"📋 share_send 响应: {send_resp}")
                        
                        data = send_resp.get("data")
                        if not data or not isinstance(data, dict):
                            logger.warning(f"⚠️ share_send 响应缺少 data 字段: {send_resp}")
                            if retry_attempt < max_share_retries:
                                await asyncio.sleep(5)
                                continue
                            else:
                                raise KeyError("data")
                        
                        batch_share_code = data.get("share_code")
                        batch_receive_code = data.get("receive_code") or data.get("recv_code")
                        
                        logger.info(f"✅ 分享分卷 {batch_idx} 创建成功: {batch_share_code}")
                        break
                        
                    except Exception as share_error:
                        error_msg = str(share_error)
                        if self._is_405_error(share_error):
                            logger.warning(f"🚫 创建分享分卷 {batch_idx} 触发 405 风控，转入分享排队: {share_error}")
                            return self._margin_limited_payload(
                                save_result,
                                "目录查询被风控(405)，已加入排队，每5分钟重试",
                                limit_reason="405",
                            )
                        if "99" in error_msg or "请重新登录" in error_msg:
                            self.is_connected = False
                            self._last_verify_failed = True
                            logger.warning(f"🔐 检测到账号登录失效 (创建分享): {share_error}")
                        
                        # data 缺失：115 非标响应，触发重试
                        is_rate_limited = isinstance(share_error, KeyError) and share_error.args and share_error.args[0] == "data"
                        if ("4100005" in error_msg or "已被移动或删除" in error_msg or is_rate_limited) and retry_attempt < max_share_retries:
                            wait = 10 if is_rate_limited else 5
                            logger.warning(f"⚠️ {'115 分享接口触发限速' if is_rate_limited else '文件尚未就绪'}，等待 {wait} 秒后重试...")
                            await asyncio.sleep(wait)
                        else:
                            logger.error(
                                f"❌ 创建分享分卷 {batch_idx} 失败: type={type(share_error).__name__}, "
                                f"args={getattr(share_error, 'args', ())}, detail={share_error}",
                                exc_info=True,
                            )
                            if batch_idx == 1: raise # If even the first batch fails, raise
                            break # Otherwise skip this batch
                
                if batch_share_code:
                    # Update share to permanent
                    try:
                        logger.info(f"🔄 正在将分享链接 {batch_share_code} 转换为长期有效...")
                        await self._api_call_with_timeout(
                            self.client.share_update_app, {"share_code": batch_share_code, "share_duration": -1},
                            async_=True, timeout=API_TIMEOUT, max_retries=2, label=f"share_update_{batch_idx}",
                            **self._get_ios_ua_kwargs()
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ 转换长期分享失败 (分卷 {batch_idx}): {e}")
                    
                    full_link = f"https://115.com/s/{batch_share_code}"
                    if batch_receive_code:
                        full_link += f"?password={batch_receive_code}"
                    share_links.append(full_link)
            
            if not share_links:
                logger.error("❌ 未能生成任何分享链接")
                return {
                    "status": "error",
                    "error_type": "share_failed",
                    "message": "未能生成任何分享链接"
                }
            
            # Format multi-link response if split occurred
            if len(share_links) > 1:
                formatted_links = []
                for idx, link in enumerate(share_links, 1):
                    formatted_links.append(f"链接 {idx}: {link}")
                result_share = "\n".join(formatted_links)
                logger.info(f"🔗 已生成 {len(share_links)} 个分卷分享链接")
            else:
                result_share = share_links[0]
                logger.info(f"🔗 长期分享链接已生成: {result_share}")
                
            return result_share
            
        except Exception as e:
            logger.error(
                f"❌ 创建新分享链接失败: type={type(e).__name__}, args={getattr(e, 'args', ())}, detail={e}",
                exc_info=True,
            )
            # 检查是否是由于违规导致的空文件夹分享失败 (errno 4100016)
            error_info = getattr(e, "args", [None, {}])[1] if hasattr(e, "args") and len(e.args) >= 2 else {}
            errno_val = error_info.get("errno") if isinstance(error_info, dict) else None
            
            if errno_val == 4100016 and save_result.get("have_vio"):
                return {
                    "status": "error",
                    "error_type": "violated",
                    "message": "链接包含违规内容，无法转存分享"
                }
            
            # 检查分享限制
            error_msg = str(e)
            if "限制分享" in error_msg:
                logger.warning(f"🚫 触发 115 分享限制")
                self.set_restriction(hours=1.0)
                return {
                    "status": "pending",
                    "reason": "restricted",
                    "share_url": save_result.get("share_url"),
                    "metadata": save_result.get("metadata", {})
                }

            # 115 接口偶发返回结构变动时，底层解析可能抛出 KeyError
            if isinstance(e, KeyError):
                missing_key = e.args[0] if e.args else "unknown"
                # margin 类 KeyError 转为 margin_limited 排队
                if missing_key == "margin":
                    return self._margin_limited_payload(
                        save_result,
                        "分享被限制（margin），已加入排队等待",
                        limit_reason="margin",
                    )
                return {
                    "status": "error",
                    "error_type": "share_response_parse_error",
                    "message": f"创建分享接口响应缺少关键字段: {missing_key}"
                }

            if self._is_405_error(e):
                logger.warning(f"🚫 创建分享阶段触发 405 风控，转入分享排队: {e}")
                return self._margin_limited_payload(
                    save_result,
                    "目录查询被风控(405)，已加入排队，每5分钟重试",
                    limit_reason="405",
                )

            if isinstance(error_info, dict) and error_info:
                api_msg = error_info.get("error") or error_info.get("msg") or error_info.get("message") or str(e)
                return {
                    "status": "error",
                    "error_type": "share_api_error",
                    "message": f"创建分享接口失败: {api_msg}"
                }

            return {
                "status": "error",
                "error_type": "share_failed",
                "message": f"创建分享失败: {error_msg}"
            }



_apply_patch()
