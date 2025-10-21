module.exports = {
  apps: [{
    name: 'fry-external-api',
    script: 'app.py',
    interpreter: 'python3',
    cwd: '/opt/fry-external-api',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '512M',
    env: {
      NODE_ENV: 'production',
      PORT: 8080,
      HOST: '0.0.0.0',
      UVICORN_RELOAD: 'false'
    },
    env_production: {
      NODE_ENV: 'production',
      PORT: 8080,
      HOST: '0.0.0.0',
      UVICORN_RELOAD: 'false'
    },
    error_file: '/var/log/fry-external-api/error.log',
    out_file: '/var/log/fry-external-api/out.log',
    log_file: '/var/log/fry-external-api/combined.log',
    time: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z'
  }]
};