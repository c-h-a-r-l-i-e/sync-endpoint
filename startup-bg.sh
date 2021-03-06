#!/bin/bash

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
 openssl \
 google-cloud-sdk

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

docker run --rm -p 443:443 -p 80:80 --name letsencrypt -v "/etc/letsencrypt:/etc/letsencrypt" -v "/var/lib/letsencrypt:/var/lib/letsencrypt" certbot/certbot certonly -n -m "mail@odk.fr.to" -d odk.fr.to --standalone --agree-tos

#Set up automatic renewal of certificates
(crontab -l 2>/dev/null; echo "1 0 * * * docker run --rm --name letsencrypt -v '/etc/letsencrypt:/etc/letsencrypt' -v '/var/lib/letsencrypt:/var/lib/letsencrypt' -v '/usr/share/nginx/html:/usr/share/nginx/html' certbot/certbot:latest renew --quiet") | crontab -

SERVICE_ACCOUNT=$(gcloud config get-value account)

# Setup keys to allow access to database
gcloud iam service-accounts keys create /.config/gcloud/application_default_credentials.json --iam-account=$SERVICE_ACCOUNT

# Create random password to allow db-transfer script access to the ODK database
openssl rand -base64 48 | tr -d '\n' | docker secret create sync-pwd -

# This is used by docker to connect to the frontend database
env INSTANCE_NAME=$(gcloud sql instances list --format "value(connectionName)") \
        docker stack deploy -c docker-compose.yml syncldap
