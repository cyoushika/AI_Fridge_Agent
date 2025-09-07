@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo 🚀 NVIDIA NeMo Agent Toolkit AI对话机器人 - Windows一键安装
echo ==================================================

REM 检查Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Python 未安装
    echo 请先安装 Python 3.12 或更高版本
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo ✅ Python 版本: %PYTHON_VERSION%

REM 检查Node.js
node --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Node.js 未安装
    echo 请先安装 Node.js 18 或更高版本
    echo 下载地址: https://nodejs.org/
    pause
    exit /b 1
)

for /f %%i in ('node --version') do set NODE_VERSION=%%i
echo ✅ Node.js 版本: %NODE_VERSION%

REM 检查Git
git --version >nul 2>&1
if errorlevel 1 (
    echo ❌ Git 未安装
    echo 请先安装 Git
    echo 下载地址: https://git-scm.com/download/win
    pause
    exit /b 1
)

echo ✅ Git 已安装

REM 安装uv包管理器
echo 📦 安装 uv 包管理器...
powershell -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 (
    echo ❌ uv 安装失败
    pause
    exit /b 1
)

REM 添加uv到PATH (临时)
set PATH=%USERPROFILE%\.local\bin;%PATH%

echo ✅ uv 包管理器已安装

REM 克隆项目
echo 📥 设置项目...
if not exist "NeMo-Agent-Toolkit" (
    echo 正在克隆 NVIDIA NeMo Agent Toolkit...
    git clone https://github.com/NVIDIA/NeMo-Agent-Toolkit.git
    cd NeMo-Agent-Toolkit
    
    echo 正在初始化子模块...
    git submodule update --init --recursive
) else (
    echo 项目目录已存在，进入目录...
    cd NeMo-Agent-Toolkit
)

REM 创建Python虚拟环境
echo 正在创建Python虚拟环境...
uv venv --seed .venv --python 3.12

REM 激活虚拟环境并安装依赖
echo 正在安装Python依赖...
call .venv\Scripts\activate.bat
uv pip install -e .
uv pip install -e .[langchain]
uv pip install tavily-python

echo ✅ 后端依赖安装完成

REM 设置前端
echo 🎨 设置前端...
cd external\aiqtoolkit-opensource-ui

echo 正在安装前端依赖...
npm install

echo ✅ 前端依赖安装完成
cd ..\..

REM 创建配置文件
echo ⚙️ 创建配置文件...
if not exist "configs" mkdir configs

(
echo # NVIDIA NeMo Agent Toolkit 黑客松配置
echo # 支持用户自定义OpenAI兼容API
echo.
echo general:
echo   use_uvloop: true
echo.
echo functions:
echo   tavily_search:
echo     _type: tavily_internet_search
echo     description: "使用Tavily API进行实时网络搜索"
echo.  
echo   current_datetime:
echo     _type: current_datetime
echo     description: "获取当前日期和时间"
echo.
echo llms:
echo   # 默认使用ModelScope API ^(用户可修改^)
echo   default_llm:
echo     _type: openai
echo     model_name: "Qwen/Qwen3-235B-A22B-Thinking-2507"
echo     api_key: "ms-89f5403e-c244-4c01-ba7e-5202eebc096a"
echo     base_url: "https://api-inference.modelscope.cn/v1"
echo     temperature: 0.7
echo     max_tokens: 2048
echo.
echo workflow:
echo   _type: react_agent
echo   tool_names:
echo     - tavily_search
echo     - current_datetime
echo   llm_name: default_llm
echo   verbose: true
echo   parse_agent_response_max_retries: 3
echo   max_iterations: 10
) > configs\hackathon_config.yml

echo ✅ 配置文件创建完成

REM 创建启动脚本
echo 📝 创建启动脚本...

(
echo @echo off
echo chcp 65001 ^>nul
echo.
echo echo 🚀 启动 NVIDIA NeMo Agent Toolkit AI对话机器人
echo echo ==============================================
echo.
echo REM 设置环境变量
echo set TAVILY_API_KEY=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
echo.
echo REM 激活Python虚拟环境
echo call .venv\Scripts\activate.bat
echo.
echo REM 启动后端服务
echo echo 📡 启动后端服务...
echo start /b aiq serve --config_file configs\hackathon_config.yml --host 0.0.0.0 --port 8001
echo.
echo REM 等待后端启动
echo echo ⏳ 等待后端服务启动...
echo timeout /t 10 /nobreak ^>nul
echo.
echo REM 启动前端服务
echo echo 🎨 启动前端服务...
echo cd external\aiqtoolkit-opensource-ui
echo start /b npm run dev
echo cd ..\..
echo.
echo echo.
echo echo ✅ 系统启动完成！
echo echo.
echo echo 🌐 访问地址:
echo echo    前端界面: http://localhost:3000
echo echo    API文档:  http://localhost:8001/docs
echo echo.
echo echo 📝 测试建议:
echo echo    1. 天气查询: '北京今天的天气怎么样，气温是多少？'
echo echo    2. 公司信息: '帮我介绍一下NVIDIA Agent Intelligence Toolkit'
echo echo    3. 时间查询: '现在几点了？'
echo echo.
echo echo 🛑 停止服务: 按 Ctrl+C 或运行 stop.bat
echo echo.
echo pause
) > start.bat

(
echo @echo off
echo chcp 65001 ^>nul
echo.
echo echo 🛑 停止 NVIDIA NeMo Agent Toolkit AI对话机器人
echo echo ==============================================
echo.
echo REM 停止相关进程
echo taskkill /f /im python.exe 2^>nul
echo taskkill /f /im node.exe 2^>nul
echo.
echo echo ✅ 所有服务已停止
echo pause
) > stop.bat

echo ✅ 启动脚本创建完成

echo.
echo 🎉 安装完成！
echo ==============
echo.
echo 📁 项目目录: %CD%
echo.
echo 🚀 快速启动:
echo    start.bat
echo.
echo 🛑 停止服务:
echo    stop.bat
echo.
echo ⚙️ 自定义配置:
echo    编辑 configs\hackathon_config.yml 文件
echo    可修改 API密钥、模型名称、base_url 等
echo.
echo 📚 更多信息:
echo    https://github.com/NVIDIA/NeMo-Agent-Toolkit
echo.
pause

