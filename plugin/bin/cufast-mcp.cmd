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
rem Order is deliberate: an explicit override, then the interpreter the build wrote
rem down, then the venv beside this repo, then PATH -- and a real error rather than
rem a silent exit if none of them work.
setlocal

rem 1. An explicit override always wins.
if defined CUFAST_PYTHON (
  "%CUFAST_PYTHON%" -m cufast.server %*
  exit /b %errorlevel%
)

rem 2. The absolute path the CMake build recorded next to this file.
rem
rem This exists because `claude plugin install` COPIES the plugin out of the repo
rem into ~/.claude/plugins/cache. The relative guess below is then two directories
rem into the cache instead of into the repo, so the installed plugin -- the install
rem the README actually recommends -- fell through to PATH every time.
set "STAMP=%~dp0interpreter.txt"
if not exist "%STAMP%" goto :beside_repo
set "PYEXE="
set /p PYEXE=<"%STAMP%"
if not defined PYEXE goto :beside_repo
if not exist "%PYEXE%" goto :beside_repo
"%PYEXE%" -m cufast.server %*
exit /b %errorlevel%

rem 3. The venv beside this repo. Only reachable when the plugin is being run from
rem the working tree, which is what --plugin-dir does.
:beside_repo
set "REPO_VENV=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%REPO_VENV%" goto :on_path
"%REPO_VENV%" -m cufast.server %*
exit /b %errorlevel%

rem 4. Whatever python PATH names, but only once it has proved it can import the
rem package. Starting the server blind here is what produced the silent failure.
:on_path
python -c "import cufast" >nul 2>&1
if errorlevel 1 goto :nowhere
python -m cufast.server %*
exit /b %errorlevel%

:nowhere
echo cufast-mcp: no Python interpreter with cufast installed. 1>&2
echo   Tried CUFAST_PYTHON, interpreter.txt beside this shim, the repo .venv, 1>&2
echo   and python on PATH. 1>&2
echo. 1>&2
echo   Build the CLI, which records the interpreter for the plugin: 1>&2
echo     cmake --build build/cli --config Release 1>&2
echo   or set CUFAST_PYTHON to the interpreter that has cufast installed. 1>&2
exit /b 1
