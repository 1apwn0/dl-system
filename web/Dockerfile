FROM python:3.12-slim
WORKDIR /app
# pip 源可注入：本地默认清华加速，海外 CI 传官方源（本地优化别成 CI 毒药）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i ${PIP_INDEX_URL}
ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA}
COPY . .

EXPOSE 8000

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]


