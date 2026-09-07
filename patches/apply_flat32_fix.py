# -*- coding: utf-8 -*-
"""
修复 P115-Share 平铺型分享（顶层条目 > 32）转存不完整导致卡死的 bug。

根因：115 的 share_snap 接口默认每页只返回 32 条，P115-Share 从不传 limit/offset、
也从不翻页，于是顶层条目 > 32 的分享（没有单一主文件夹）只会被转存前 32 个，
体积永远追不上基准；而比对逻辑的两个完成分支都要求 top_match，兜底分支也被堵死，
最终空转到轮询上限卡死。

修复：
  A. _share_snap_with_fallback 显式传 limit/offset/cid 并按 count 自动翻页取全
  B. 比对循环增加二级兜底：体积长期停滞即判定完成，不再死等 top_match
"""
import shutil, sys, time, py_compile

PATH = "/app/app/services/p115.py"
BAK = "/app/app/services/p115.py.bak_" + time.strftime("%Y%m%d_%H%M%S")

src = open(PATH, encoding="utf-8").read()
orig_len = len(src)
shutil.copy2(PATH, BAK)
print(f"[0] 已备份 -> {BAK}  ({orig_len} 字节)")

# ---------------------------------------------------------------- A1 ----------
A1_OLD = '''    async def _share_snap_with_fallback(self, payload: dict, **kwargs) -> dict:
        """
        多端点容错的 share_snap 调用
        策略：优先 app 接口（proapi），失败自动降级到 webapi
        """'''
A1_NEW = '''    async def _share_snap_with_fallback(self, payload: dict, **kwargs) -> dict:
        """
        多端点容错的 share_snap 调用
        策略：优先 app 接口（proapi），失败自动降级到 webapi

        [FIX 平铺型分享] 115 的 share_snap 默认每页只返回 32 条。顶层条目 > 32 的
        分享（没有单一主文件夹、文件直接铺在分享根目录）会被静默截断，只转存前 32 个，
        体积永远追不上基准而卡死。这里显式指定 limit/offset 并按 count 自动翻页取全。
        """
        payload = dict(payload or {})
        payload.setdefault("limit", 1000)
        payload.setdefault("offset", 0)
        payload.setdefault("cid", 0)'''
assert src.count(A1_OLD) == 1, f"A1 锚点命中 {src.count(A1_OLD)} 次，应为 1"
src = src.replace(A1_OLD, A1_NEW, 1)
print("[A1] 函数签名 + payload 注入  OK")

# ---------------------------------------------------------------- A2 ----------
A2_OLD = '''        return await self._call_endpoints_with_fallback(
            endpoints, timeout=30, label="share_snap"
        )
'''
A2_NEW = '''        resp = await self._call_endpoints_with_fallback(
            endpoints, timeout=30, label="share_snap"
        )
        return await self._paginate_share_snap(payload, resp, **kwargs)

    async def _paginate_share_snap(self, payload: dict, first_resp: dict, **kwargs) -> dict:
        """share_snap 自动翻页：首页不足 count 时继续翻，合并 list 后返回。"""
        try:
            def _inner(r):
                d = r.get("data") if isinstance(r.get("data"), dict) else r
                return d if isinstance(d, dict) else {}

            d0 = _inner(first_resp)
            try:
                total = int(d0.get("count") or 0)
            except (TypeError, ValueError):
                total = 0
            base = d0.get("list") or []
            if total <= 0 or not base or len(base) >= total:
                return first_resp

            merged = list(base)
            seen = set(str(i.get("fid") or i.get("cid")) for i in base)
            offset = len(base)
            page_size = 1000
            for _ in range(50):
                if len(merged) >= total:
                    break
                page_payload = dict(payload)
                page_payload["limit"] = page_size
                page_payload["offset"] = offset
                page_endpoints = [
                    {
                        "name": "share_snap_app",
                        "func": lambda p=page_payload: self.client.share_snap_app(
                            p, base_url="https://proapi.115.com", async_=True,
                            **self._get_ios_ua_kwargs(), **kwargs),
                        "base_url": "https://proapi.115.com",
                    },
                    {
                        "name": "share_snap_webapi",
                        "func": lambda p=page_payload: self._share_snap_webapi(p, **kwargs),
                        "base_url": "https://webapi.115.com",
                    },
                ]
                try:
                    r = await self._call_endpoints_with_fallback(
                        page_endpoints, timeout=30, label="share_snap_page")
                except Exception as pg_err:
                    logger.warning(f"⚠️ share_snap 翻页失败 (offset={offset}): {pg_err}")
                    break
                chunk = _inner(r).get("list") or []
                if not chunk:
                    break
                added = 0
                for it in chunk:
                    key = str(it.get("fid") or it.get("cid"))
                    if key not in seen:
                        seen.add(key)
                        merged.append(it)
                        added += 1
                if added == 0:
                    break
                offset += len(chunk)
                if len(chunk) < page_size:
                    break

            logger.info(
                f"📄 share_snap 自动翻页: 首页 {len(base)} 条 -> 合计 {len(merged)} 条 "
                f"(count={total})"
            )
            if isinstance(first_resp.get("data"), dict):
                first_resp["data"]["list"] = merged
            else:
                first_resp["list"] = merged
            return first_resp
        except Exception as e:
            logger.warning(f"⚠️ share_snap 翻页处理异常，返回原始结果: {e}")
            return first_resp
'''
assert src.count(A2_OLD) == 1, f"A2 锚点命中 {src.count(A2_OLD)} 次，应为 1"
src = src.replace(A2_OLD, A2_NEW, 1)
print("[A2] 翻页逻辑注入  OK")

# ---------------------------------------------------------------- B ----------
B_OLD = '''                        new_fids = [item["id"] for item in current_items]
                        size_stagnant_done = True
                        break
'''
B_NEW = '''                        new_fids = [item["id"] for item in current_items]
                        size_stagnant_done = True
                        break
                    elif (
                        current_size > 0
                        and current_total > 0
                        and size_stagnant_times >= 12
                    ):
                        # 二级兜底：源分享统计口径不一致（如平铺型分享只统计到部分条目）
                        # 导致顶层结构长期不匹配时，体积长期停滞即按当前内容判定完成，
                        # 避免空转到轮询上限把任务彻底卡死。
                        logger.warning(
                            f"⏹️ 体积连续 {size_stagnant_times} 轮停滞且顶层结构未匹配 "
                            f"({len(current_items)}/{len(names)})，按当前内容判定完成，避免死循环"
                        )
                        new_fids = [item["id"] for item in current_items] or [to_cid]
                        size_stagnant_done = True
                        break
'''
assert src.count(B_OLD) == 1, f"B 锚点命中 {src.count(B_OLD)} 次，应为 1"
src = src.replace(B_OLD, B_NEW, 1)
print("[B] 比对循环二级兜底  OK")

open(PATH, "w", encoding="utf-8").write(src)
print(f"[✓] 已写回 {PATH}  ({orig_len} -> {len(src)} 字节)")

# 语法校验
try:
    py_compile.compile(PATH, doraise=True)
    print("[✓] 语法校验通过")
except Exception as e:
    print(f"[✗] 语法错误，回滚: {e}")
    shutil.copy2(BAK, PATH)
    sys.exit(1)
