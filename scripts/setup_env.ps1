# Bloomberg PC 환경변수 영구 등록 (최초 1회만 실행)
[Environment]::SetEnvironmentVariable("BBG_ENABLED", "1", "User")
[Environment]::SetEnvironmentVariable("SUPABASE_URL", "https://sxkrwlgggkcsfjqwyntb.supabase.co", "User")
[Environment]::SetEnvironmentVariable("SUPABASE_SERVICE_ROLE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN4a3J3bGdnZ2tjc2ZqcXd5bnRiIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTgxNTkzNywiZXhwIjoyMDk1MzkxOTM3fQ.nPFw_6-168brsadxY7obYzU6xV-4Kp7E2XKimOqBAi0", "User")
Write-Host "환경변수 등록 완료. 터미널 재시작 후 확인하세요."
