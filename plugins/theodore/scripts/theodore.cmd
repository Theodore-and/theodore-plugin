@echo off
rem Theodore's command in the plugin on Windows, which the host starts by its extension when the plugin carries
rem no theodore.exe (contract C0-2, W4): the native front when it is here, else the skill's launcher, as before
rem the front.
if exist "%~dp0theodore.exe" goto front
"%~dp0..\skills\pptx\scripts\theodore.cmd" %*
exit /b %ERRORLEVEL%
:front
"%~dp0theodore.exe" %*
exit /b %ERRORLEVEL%
