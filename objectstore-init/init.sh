#!/bin/sh
set -eu

S3="aws --endpoint-url http://objectstore:8333 s3api"

until $S3 list-buckets >/dev/null 2>&1; do
  echo "waiting for objectstore..."; sleep 2
done

for b in invoices attachments assets; do
  $S3 head-bucket --bucket "$b" 2>/dev/null || $S3 create-bucket --bucket "$b"
done
$S3 head-bucket --bucket audit-worm 2>/dev/null || \
  $S3 create-bucket --bucket audit-worm --object-lock-enabled-for-bucket

for b in invoices attachments; do
  $S3 put-bucket-versioning --bucket "$b" --versioning-configuration Status=Enabled
done

$S3 put-object-lock-configuration --bucket audit-worm \
  --object-lock-configuration '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"COMPLIANCE","Days":1}}}'

# Verify, don't trust: a versioned bucket must return a version id
for b in invoices attachments; do
  n=0
  while :; do
    n=$((n+1))
    v=$($S3 put-object --bucket "$b" --key .init-probe --query VersionId --output text 2>/dev/null || echo None)
    if [ -n "$v" ] && [ "$v" != "None" ]; then
      $S3 delete-object --bucket "$b" --key .init-probe --version-id "$v" >/dev/null
      echo "OK: $b versioned (attempt $n)"
      break
    fi
    if [ "$n" -ge 10 ]; then
      echo "FAIL: $b returned no version id after $n attempts" >&2
      exit 1
    fi
    sleep 2
  done
done

mode=$($S3 get-object-lock-configuration --bucket audit-worm --query 'ObjectLockConfiguration.ObjectLockEnabled' --output text)
[ "$mode" = "Enabled" ] || { echo "FAIL: audit-worm lock not enabled" >&2; exit 1; }
echo "OK: audit-worm object lock enabled"