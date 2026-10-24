provider "aws" {
  region = var.aws_region
}

# Amazon Q Developer in chat applications (AWS Chatbot) only has its API in a
# few regions, us-east-1 not among them. Only chatbot.tf uses this.
provider "aws" {
  alias  = "chatbot"
  region = var.chatbot_region
}
