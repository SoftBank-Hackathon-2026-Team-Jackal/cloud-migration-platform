# Ticket 54 (#189): build-time instance-metadata probe. The build SUCCEEDS only when a RUN step
# cannot reach the metadata service. It prints HTTP status codes only, never a token or credential.
FROM curlimages/curl:8.11.1
USER root
RUN token_status="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 -m 5 -X PUT \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token || true)"; \
    role_status="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 -m 5 \
        http://169.254.169.254/latest/meta-data/iam/security-credentials/ || true)"; \
    echo "imds_probe token_put=${token_status:-000} role_list=${role_status:-000}"; \
    test "${token_status:-000}" = 000 && test "${role_status:-000}" = 000
