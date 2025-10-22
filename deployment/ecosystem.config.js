module.exports = {
  apps: [{
  name: 'hardware_exe_api',
  script: 'deployment/start_prod.sh',
  interpreter: 'bash',
  cwd: '/opt/hardware_exe_api',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '512M',
    // Minimal defaults; canonical environment comes from /opt/hardware_exe_api/.env
    env: {},
    env_production: {},
  error_file: '/var/log/hardware_exe_api/error.log',
  out_file: '/var/log/hardware_exe_api/out.log',
  log_file: '/var/log/hardware_exe_api/combined.log',
    time: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z'
  }]
};