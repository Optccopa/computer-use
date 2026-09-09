@echo off
rem Starts the cufast MCP server for the Claude Code plugin.
rem
rem A shim rather than a bare "python -m cufast.server" in .mcp.json because the
rem interpreter that has cufast installed is almost never the one on PATH. Claude
rem Code launches MCP servers with its own environment, not the shell's, so a
rem virtualenv the user had activated when they installed the package is not
rem active here and "python" resolves to whatever came first in the system PATH.
rem That failure is silent and confusing: the server exits with ModuleNotFoundError
rem and the tool simply never appears.
rem
rem Order is deliberate: an explicit override first, then the venv that sits beside
rem this repo, then PATH as a last resort for a global install.
setlocal

if defined CUFAST_PYTHON (
  "%CUFAST_PYTHON%" -m cufast.server %*
  exit /b %errorlevel%
)

rem %~dp0 is this file's directory, so ..\.. is the repository root.
set "REPO_VENV=%~dp0..\..\.venv\Scripts\python.exe"
if exist "%REPO_VENV%" (
  "%REPO_VENV%" -m cufast.server %*
  exit /b %errorlevel%
)

python -m cufast.server %*
exit /b %errorlevel%
