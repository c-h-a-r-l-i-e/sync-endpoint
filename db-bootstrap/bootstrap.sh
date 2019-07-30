#!/bin/sh -u

set -o pipefail

echo "ODK Sync Database Bootstrap Script"

if [ ${DB_BOOTSTRAP} != true ]; then
    echo 'DB_BOOTSTRAP not set to true'
    echo 'Database bootstrap cancelled'
    exit 0
fi;

# sleep 10 seconds to wait for db service to be created
# TODO: test for it in a loop instead
sleep 10

STACK_NAME=$(docker ps -f "ID=${HOSTNAME}" --format '{{ .Names }}' | grep -oE '^[^_]*')
DB_VAR=$(docker service inspect -f "{{if eq \"${STACK_NAME}_db\" .Spec.Name}}{{.Spec.TaskTemplate.ContainerSpec.Image}}{{end}}" $(docker service ls -q) | grep -oE '^[^:]*')
DB_CONTAINER_ID=$(docker ps -f "volume=${STACK_NAME}_db-vol" --format '{{.ID}}')
LDAP_CONTAINER_ID=$(docker ps -f "label=com.docker.swarm.service.name=syncldap_ldap-service" --format '{{.ID}}')

# try to establish tcp connection with db, each connection timesout after 1 second 
WAIT_CMD="while ! nc -w 1 db \${DB_PORT} < /dev/null; do echo 'waiting'; sleep 1; done"

case ${DB_VAR} in
    postgres)
        echo "postgres db detected"
        DB_PORT=5432
        eval ${WAIT_CMD}
	sleep 20
	echo "Db container id ${DB_CONTAINER_ID}"
        docker exec ${DB_CONTAINER_ID} psql \
            -c 'CREATE USER odk WITH UNENCRYPTED PASSWORD '\''odk'\'';' \
            -c 'CREATE SCHEMA odk_sync AUTHORIZATION odk;' \
            -c 'GRANT ALL PRIVILEGES ON SCHEMA odk_sync TO odk;' \
            -U ${POSTGRES_USER} -d ${POSTGRES_DB}
        ;;
    mysql)
        echo "mysql db detected"
        DB_PORT=3306
        eval ${WAIT_CMD}
        docker exec ${DB_CONTAINER_ID} mysql \
            -e 'CREATE DATABASE odk_sync;' \
            -e 'GRANT USAGE ON *.* TO odk@% IDENTIFIED BY '\''odk'\'';' \
            -e 'GRANT ALL PRIVILEGES ON odk_sync.* TO odk@%;' \
            -p"${MYSQL_ROOT_PASSWORD}"
        ;;
    sqlserver)
        echo "sqlserver db detected"
        ;;
    *)
        echo "Error"
        echo "${DB_VAR} not supported"
        ;;
esac

echo "Done"

echo "Checking Sync endpoint"

# Wait 5 seconds for a 200 from Sync
timeout 5 sh -c 'while ! echo -ne "GET / HTTP/1.1\nHost: sync\n\n" | nc -w 1 sync 8080 | grep -q "HTTP/1.1 200"; do echo '\''waiting for Sync'\''; sleep 1; done'

if [ $? -eq 143 ]; then
    echo "Timeout"
    echo "Killing Sync endpoint"
    # let Docker restart the container
    docker kill $(docker ps -f "label=com.docker.swarm.service.name=${STACK_NAME}_sync" --format '{{.ID}}')
fi;

# setup ldap
echo "Setting up LDAP container"

docker exec ${LDAP_CONTAINER_ID} ldappasswd -D "cn=admin,dc=example,dc=org" \
	-w admin -S "uid=dbTransfer,ou=people,dc=example,dc=org" \
	-s $(cat /run/secrets/sync-pwd)

echo "Done"


echo "Exit"

exit 0
