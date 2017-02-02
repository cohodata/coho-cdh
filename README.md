Docker Image Manifests for Coho Data Compute Cluster
====================================================
This repository includes the required utilities for creating
a simple Big Data Compute Cluster to run on Coho Datastream.

Before starting, you will need:

  - the location of the tenant's docker portal
    ie. tcp://<portal_IP>:<port>

  - the yarn image on the tenant's docker registry
    ie. <registry_ip>:5000/cohodata/yarn:5.0


In order to use these docker images you will need to pull it down
from the docker hub and push it to the tenant registry:

    docker pull cohodata/yarn:5.0
    docker tag cohodata/yarn:5.0 <registry_ip>:5000/cohodata/yarn:5.0
    docker push <registry_ip>:5000/cohodata/yarn:5.0
  
then run the deployment script from a container with the image:

    docker run --rm -ti cohodata/yarn:5.0 deploy-cdh-cluster --docker-portal=<portal_addr> --yarn-image=<registry_ip>:5000/cohodata/yarn:5.0 create <number_of_nodemanagers>

