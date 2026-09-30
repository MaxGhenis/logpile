import os

# Sync defaults to a wall-clock budget and a free-space guard (40 GiB, or
# 60 GiB beside a Time Machine snapshot). Test machines and CI runners rarely
# have that much free space, and a budget would make results timing-
# dependent, so the suite opts out; tests of those limits pass SyncLimits
# explicitly.
os.environ.setdefault("LOGPILE_SYNC_DISK_GUARD", "0")
os.environ.setdefault("LOGPILE_SYNC_BUDGET_SECONDS", "0")
