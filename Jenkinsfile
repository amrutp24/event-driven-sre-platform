pipeline {
  agent any
  environment {
    AWS_REGION = "us-east-1"
    ECR_REPO   = "<YOUR_ECR_REPO>/checkout"
    IMAGE_TAG  = "${env.BUILD_NUMBER}"
  }
  stages {
    stage("Build") {
      steps {
        sh "docker build -t ${ECR_REPO}:${IMAGE_TAG} apps/checkout"
      }
    }
    stage("Scan") {
      steps {
        sh "trivy image --exit-code 1 --severity HIGH,CRITICAL ${ECR_REPO}:${IMAGE_TAG}"
      }
    }
    stage("Push") {
      steps {
        sh '''
          aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin $(echo ${ECR_REPO} | cut -d/ -f1)
          docker push ${ECR_REPO}:${IMAGE_TAG}
        '''
      }
    }
    stage("Deploy") {
      steps {
        sh '''
          helm upgrade --install checkout helm/checkout             --set image.repository=${ECR_REPO}             --set image.tag=${IMAGE_TAG}
        '''
      }
    }
    stage("Guardrail") {
      steps {
        sh "echo Guardrail placeholder: query SLO burn / synthetics; rollback if needed."
      }
    }
  }
}
