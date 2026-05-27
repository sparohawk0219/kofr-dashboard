@echo off
cd /d c:\projects\KOFR
python -c "from pipeline.pusher import push_snapshot; push_snapshot()"
