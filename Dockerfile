# Dockerfile for CDH5 images
#
# Copyright (c) 2016 Coho Data Inc.
# The subject matter distributed under this license is or is based on
# information and material generated by Coho Data Inc. It may only be
# acquired, used, modified and distributed under the terms of the Coho
# Data Compute Cluster License v1.0.  Except as permitted in the Coho
# Data Compute Cluster License v1.0, all other rights are reserved in
# any copyright or other similar rights which may exist. Execution of
# software distributed under this Coho Data Compute Cluster License
# v1.0 may cause you to acquire third-party software (as described in
# the accompanying documentation) and you agree (a) to comply with the
# applicable licenses thereunder and (b) that Coho is not responsible
# in any way for your compliance or non-compliance with the applicable
# third-party licenses or the consequences of your being subject to
# said licenses or your compliance or non-compliance.
#
# Installs:
# - Oracle JDK 7
# - CDH5 Hadoop
# - Consul agent
# - some utilities

FROM ubuntu:14.04.4

MAINTAINER support@cohodata.com

RUN apt-get update -y
RUN apt-get install -y curl

RUN curl -s http://archive.cloudera.com/cdh5/ubuntu/precise/amd64/cdh/archive.key | apt-key add -
ADD https://archive.cloudera.com/cdh5/debian/wheezy/amd64/cdh/cloudera.list /etc/apt/sources.list.d/cloudera-cdh5.list 
ADD https://archive.cloudera.com/cm5/ubuntu/trusty/amd64/cm/cloudera.list  /etc/apt/sources.list.d/cloudera-cm5.list

RUN apt-get update -y

RUN apt-get install -y ant \
                       unzip \
                       wget \
                       lbzip2 \
                       vim \
                       emacs \
                       rsync \
                       iputils-ping \
                       jq \
                       net-tools \
                       openssh-server \
                       python \
                       oracle-j2sdk1.7 \
                       hadoop-yarn-resourcemanager \
                       hadoop-yarn-nodemanager \
                       hadoop-mapreduce \
                       hadoop-mapreduce-historyserver \
                       hadoop-0.20-mapreduce-jobtracker \
                       hadoop-0.20-mapreduce-tasktracker

# Add Hadoop configurations
ADD etc/hadoop/conf.docker.yarn /etc/hadoop/conf.docker.yarn
ENV LOGGER_ENV_VAR "INFO,console"

# Add Hadoop scripts
ADD usr/local/bin/cio-hadoop-run                          /usr/local/bin/cio-hadoop-run
ADD usr/local/bin/cio-hadoop-topology                     /usr/local/bin/cio-hadoop-topology
ADD usr/lib/hadoop-yarn/sbin/yarn-daemon.sh               /usr/lib/hadoop-yarn/sbin/yarn-daemon.sh
ADD usr/lib/hadoop-mapreduce/sbin/mr-jobhistory-daemon.sh /usr/lib/hadoop-mapreduce/sbin/mr-jobhistory-daemon.sh

# Install consul
ADD https://releases.hashicorp.com/consul/0.5.2/consul_0.5.2_linux_amd64.zip /tmp/consul.zip
RUN unzip /tmp/consul.zip -d /usr/bin/

# Consul
ADD usr/bin/do-with-consul                                              /usr/bin/do-with-consul
ADD etc/consul.d  /etc/consul.d
RUN mkdir -p /var/consul/data
ENV WITH_CONSUL true
# The retry interval and retry max control how long the
# consul agent will attempt to retry joining the cluster.
# NB: CONSUL_RETRY_MAX==0 causes the agent to retry indefinately.
ENV CONSUL_RETRY_INTERVAL 30
ENV CONSUL_RETRY_MAX 10

# Cluster configuration parameters used to qualify consul node lookups.
# COHO_TENANT: the name of tenant network that this node is a member of
# COHO_HADOOP_CLUSTER: the name of the hadoop cluster which this node is a member of
# WITH_COHO_HADOOP_TOPOLOGY: set to false to disable container topology location

# For example the FQDN of a node with hostname resourcemanager node be:
# resourcemanager.${COHO_HADOOP_CLUSTER}.${COHO_TENANT}.node.dc1.consul

ENV COHO_TENANT namespace1
ENV COHO_HADOOP_CLUSTER dc
ENV WITH_COHO_HADOOP_TOPOLOGY true

# This image will be used to set up a Yarn cluster
RUN rm -rf /etc/hadoop/conf && \
    mv /etc/hadoop/conf.docker.yarn /etc/hadoop/conf

ENTRYPOINT ["/usr/bin/do-with-consul"]
