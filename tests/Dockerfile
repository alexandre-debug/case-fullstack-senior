FROM python:3.12-slim
WORKDIR /tests
COPY requirements.txt .
RUN pip install -r requirements.txt
# O código da API e do worker é montado em tempo de execução (docker-compose.yml), para os testes
# exercitarem exatamente o que roda em produção.
# no:cacheprovider porque /tests é montado somente-leitura.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
