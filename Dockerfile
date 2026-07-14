FROM python:3.13-slim

# Instala curl e unzip (necessários para instalar o Deno) junto com o ffmpeg
RUN apt update && apt install -y ffmpeg curl unzip

# Baixa e instala o Deno oficial
RUN curl -fsSL https://deno.land/install.sh | sh

# Coloca o Deno no PATH do sistema para o Gunicorn e o yt-dlp o encontrarem
ENV PATH="/root/.deno/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

CMD ["gunicorn", "--timeout", "300", "--workers", "1", "app:app"]
