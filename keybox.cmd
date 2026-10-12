@echo off
rem keybox 的 Windows 入口。把这个目录加进 PATH，就能在任何地方敲 keybox。
rem   keybox list
rem   keybox add demo/stripe --url ... --secret ...
rem   keybox mcp
python "%~dp0keybox.py" %*
