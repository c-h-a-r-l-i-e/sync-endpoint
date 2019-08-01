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
 git \
 openssl

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
mvn -Dmaven.test.skip=true clean install
cd ..
docker build --pull -t odk/sync-web-ui https://github.com/opendatakit/sync-endpoint-web-ui.git
docker build -t odk/db-bootstrap db-bootstrap
docker build --pull -t odk/openldap openldap
docker build --pull -t odk/phpldapadmin phpldapadmin
docker build -t odk/db-transfer db-transfer

SERVICE_ACCOUNT=$(gcloud config get-value account)

# Setup keys to allow access to database
gcloud iam service-accounts keys create ~/.config/gcloud/application_default_credentials.json --iam-account=$SERVICE_ACCOUNT

# Create random password to allow db-transfer script access to the ODK database
openssl rand -base64 48 | tr -d '\n' | docker secret create sync-pwd -

# This is used by docker to connect to the frontend database
export INSTANCE_NAME=$(gcloud sql instances list --format "value(connectionName)")

docker stack deploy -c docker-compose.yml syncldap
