FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y python3 curl jq sudo git util-linux ca-certificates
COPY . /work
RUN chmod 755 /work/tests/install-smoke.sh
CMD ["bash", "/work/tests/install-smoke.sh"]
