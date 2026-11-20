# 1. 删除自动检查点 ref（不再需要该快照时）
git update-ref -d refs/codex/turn-diffs/checkpoints/0d4c5fb6b41afb701e58648b3eadb504/85824072422e7e653b69e4cb761b787d/1795139521814/f52af5bb-7f90-4c16-b43d-419bfeb7acd1

# 2. 立即清理不可达对象并打包
git reflog expire --expire=now --all
git gc --prune=now
