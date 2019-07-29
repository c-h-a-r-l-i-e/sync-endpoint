#!/bin/bash
if [ -f /root/sync-endpoint]; then
  echo "System already configured. Skipping script"
  exit 0
fi

DEBIAN_FRONTEND=noninteractive

add-apt-repository -y universe
add-apt-repository -y ppa:certbot/certbot
apt-get -y update
apt-get -y remove openjdk-11-jre-headless
apt-get -y install \
 zip \
 unzip \
 wget \
 curl \
 openjdk-8-jdk-headless \
 software-properties-common \
 maven \
 git

curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -
add-apt-repository -y \
	   "deb [arch=amd64] https://download.docker.com/linux/ubuntu \
	      $(lsb_release -cs) \
	         stable"
apt-get -y update
apt-get -y install docker-ce

unattended-upgrades
apt-get -y autoremove

git clone https://github.com/c-h-a-r-l-i-e/sync-endpoint

cd sync-endpoint/sync-endpoint
docker swarm init
mvn clean install
cd ..
docker build --pull -t odk/sync-web-ui https://github.com/opendatakit/sync-endpoint-web-ui.git
docker build -t odk/db-bootstrap db-bootstrap
docker build --pull -t odk/openldap openldap
docker build --pull -t odk/phpldapadmin phpldapadmin
docker build -t odk/db-transfer db-transfer

docker stack deploy -c docker-compose.yml syncldap