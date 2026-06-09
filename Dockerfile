FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用文件
COPY server_deploy.py .
COPY weibotop_api.py .
COPY index.html .
COPY data.json .
COPY archive/ archive/

# 端口（Railway 通过 PORT 环境变量注入）
EXPOSE 18765

# 启动
CMD ["python", "server_deploy.py"]
