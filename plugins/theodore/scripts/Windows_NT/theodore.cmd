@echo off
rem Theodore's command in the plugin on Windows; the plugin's MCP entry names scripts\%OS%\theodore and the host
rem finds theodore.exe here when the plugin carries the signed front, else this file. It runs the front when it is here,
rem else the skill's launcher, as before the front.
if exist "%~dp0theodore.exe" goto front
"%~dp0..\..\skills\pptx\scripts\theodore.cmd" %*
exit /b %ERRORLEVEL%
:front
"%~dp0theodore.exe" %*
exit /b %ERRORLEVEL%
