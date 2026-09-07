FROM listeningltg/p115-share:latest

# 修复「平铺型分享（顶层条目 > 32）转存卡死」的 bug。
# 官方 latest 镜像内置的 /app/app/services/p115.py 不带此修复，
# 这里在构建时对其打补丁；补丁用锚点断言，若上游 p115.py 变更会直接构建失败，
# 便于及时发现上游不兼容，而不是静默跳过修复。
COPY patches/apply_flat32_fix.py /tmp/apply_flat32_fix.py
RUN python /tmp/apply_flat32_fix.py \
    && rm -f /tmp/apply_flat32_fix.py /app/app/services/p115.py.bak_*
