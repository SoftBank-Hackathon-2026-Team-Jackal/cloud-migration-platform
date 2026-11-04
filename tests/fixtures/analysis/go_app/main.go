package main

import (
	"context"
	"net/http"
	"os"

	"github.com/aws/aws-sdk-go-v2/service/s3"
	sqsv2 "github.com/aws/aws-sdk-go-v2/service/sqs"
)

func main() {
	dbURL := os.Getenv("DATABASE_URL")
	name := getenv("ENV_NAME", "local")
	if v, ok := os.LookupEnv("FEATURE_X"); ok {
		_ = v
	}
	// os.Getenv("COMMENTED_OUT")
	ctx := context.Background()
	_, _ = client.PutObject(ctx, &s3.PutObjectInput{Bucket: &bucket})
	_, _ = queue.SendMessage(ctx, &sqsv2.SendMessageInput{})
	_, _ = http.Get("https://api.payments.example.com/v1/charge?key=abc123")
	req, _ := http.NewRequest("POST", "http://internal-billing.example.net:8443/x", nil)
	_, _ = http.Get("http://localhost:8080/healthz")
	_, _, _ = dbURL, name, req
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
