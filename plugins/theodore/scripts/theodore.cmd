@echo off
rem Theodore's command in the plugin on Windows; the plugin's MCP entry runs it with "mcp". The native front
rem beside it when this plugin carries one (it does once the front is signed), else the skill's launcher, as
rem before the front.
if exist "%~dp0theodore.exe" goto front
"%~dp0..\skills\pptx\scripts\theodore.cmd" %*
exit /b %ERRORLEVEL%
:front
"%~dp0theodore.exe" %*
exit /b %ERRORLEVEL%
