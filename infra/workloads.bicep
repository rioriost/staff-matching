targetScope = 'resourceGroup'

param location string = resourceGroup().location
param image string
param deployApplication bool = false
param verificationOnly bool = false
param setupAccounts bool = false
param accountAuthentication bool = false
param accountOrigin string = ''
param setupSaas bool = false
param saasEnabled bool = false
param deployWorker bool = false
@description('Retain this existing revision at 100% while validating the SaaS preview label.')
param retainedRevision string = ''
var suffix = take(uniqueString(resourceGroup().id), 8)
var pgHost = 'psql-staff-${suffix}.postgres.database.azure.com'
var vaultUri = 'https://kv-staff-${suffix}${environment().suffixes.keyvaultDns}/'
var tags = { project: 'staff-matching', environment: 'development', data: 'synthetic-only' }

resource containerEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: 'cae-staff-${suffix}'
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'acrstaff${suffix}'
}
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-staff-app-${suffix}'
}
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-staff-setup-${suffix}'
}
resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-staff-worker-${suffix}'
}
var pgEnvironment = [
  { name: 'PGHOST', value: pgHost }
  { name: 'PGDATABASE', value: 'staffmatching' }
  { name: 'PGPORT', value: '5432' }
  { name: 'PGSSLMODE', value: 'verify-full' }
  { name: 'PGSSLROOTCERT', value: '/etc/ssl/certs/ca-certificates.crt' }
  { name: 'PGCONNECT_TIMEOUT', value: '15' }
]
resource bootstrap 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-staff-setup-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${setupIdentity.id}': {} }
  }
  properties: {
    environmentId: containerEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: setupSaas ? 1800 : 600
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{
        server: registry.properties.loginServer
        identity: setupIdentity.id
      }]
      secrets: concat([
        { name: 'pg-admin', keyVaultUrl: '${vaultUri}secrets/postgres-admin-password', identity: setupIdentity.id }
        { name: 'pg-app', keyVaultUrl: '${vaultUri}secrets/app-database-password', identity: setupIdentity.id }
      ], (setupAccounts || setupSaas) ? [
        { name: 'account-seed', keyVaultUrl: '${vaultUri}secrets/poc-account-bootstrap', identity: setupIdentity.id }
      ] : [], setupSaas ? [
        { name: 'pg-worker', keyVaultUrl: '${vaultUri}secrets/worker-database-password', identity: setupIdentity.id }
      ] : [])
    }
    template: {
      containers: [{
        name: 'setup'
        image: image
        command: ['python', '-m', setupSaas ? 'backend.saas_setup' : (setupAccounts ? 'backend.poc_accounts' : 'backend.postgres_setup')]
        args: verificationOnly ? ['--verify'] : []
        resources: { cpu: setupSaas ? json('1.0') : json('0.5'), memory: setupSaas ? '2Gi' : '1Gi' }
        env: concat(pgEnvironment, [
          { name: 'PGUSER', value: 'staffadmin' }
          { name: 'PGPASSWORD', secretRef: 'pg-admin' }
          { name: 'POC_APP_DB_PASSWORD', secretRef: 'pg-app' }
        ], (setupAccounts || setupSaas) ? [
          { name: 'POC_ACCOUNT_SEED', secretRef: 'account-seed' }
          { name: 'POC_AUTH_ORIGIN', value: accountOrigin }
        ] : [], setupSaas ? [
          { name: 'POC_WORKER_DB_PASSWORD', secretRef: 'pg-worker' }
        ] : [])
      }]
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApplication) {
  name: 'ca-staff-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${appIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: empty(retainedRevision) ? 'Single' : 'Multiple'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: empty(retainedRevision) ? [
          { latestRevision: true, weight: 100 }
        ] : [
          { revisionName: retainedRevision, weight: 100 }
          { latestRevision: true, weight: 0, label: 'saas-preview' }
        ]
      }
      registries: [{
        server: registry.properties.loginServer
        identity: appIdentity.id
      }]
      secrets: concat([
        { name: 'pg-app', keyVaultUrl: '${vaultUri}secrets/app-database-password', identity: appIdentity.id }
      ], accountAuthentication ? [] : [
        { name: 'poc-auth', keyVaultUrl: '${vaultUri}secrets/poc-access-password', identity: appIdentity.id }
      ])
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: saasEnabled ? 2 : 1
        rules: [{
          name: 'http-requests'
          http: { metadata: { concurrentRequests: '20' } }
        }]
      }
      containers: [{
        name: 'staff-matching'
        image: image
        resources: { cpu: saasEnabled ? json('1.0') : json('0.5'), memory: saasEnabled ? '2Gi' : '1Gi' }
        env: concat(pgEnvironment, [
          { name: 'APP_MODE', value: 'azure-poc' }
          // Only managed HTTPS ingress can reach this container from the public internet.
          { name: 'FORWARDED_ALLOW_IPS', value: '*' }
          { name: 'PGUSER', value: 'staff_app' }
          { name: 'PGPASSWORD', secretRef: 'pg-app' }
          { name: 'SAAS_ENABLED', value: saasEnabled ? 'true' : 'false' }
          { name: 'SAAS_DEMO_SIMULATION', value: saasEnabled ? 'true' : 'false' }
        ], accountAuthentication ? [
          { name: 'POC_AUTH_MODE', value: 'accounts' }
          { name: 'POC_AUTH_ORIGIN', value: accountOrigin }
        ] : [
          { name: 'POC_AUTH_MODE', value: 'basic' }
          { name: 'STAFF_MATCHING_TENANT', value: 'demo' }
          { name: 'POC_AUTH_USERNAME', value: 'coordinator' }
          { name: 'POC_AUTH_PASSWORD', secretRef: 'poc-auth' }
        ])
        probes: [
          {
            type: 'Startup'
            httpGet: { path: '/healthz', port: 8000, scheme: 'HTTP' }
            periodSeconds: 5
            failureThreshold: 30
          }
          {
            type: 'Liveness'
            httpGet: { path: '/healthz', port: 8000, scheme: 'HTTP' }
            periodSeconds: 30
          }
          {
            type: 'Readiness'
            httpGet: { path: '/healthz', port: 8000, scheme: 'HTTP' }
            periodSeconds: 10
          }
        ]
      }]
    }
  }
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = if (deployWorker) {
  name: 'ca-staff-worker-${suffix}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${workerIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerEnvironment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [{
        server: registry.properties.loginServer
        identity: workerIdentity.id
      }]
      secrets: [{
        name: 'pg-worker'
        keyVaultUrl: '${vaultUri}secrets/worker-database-password'
        identity: workerIdentity.id
      }]
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 2
        rules: [{
          name: 'worker-cpu'
          custom: { type: 'cpu', metadata: { type: 'Utilization', value: '70' } }
        }]
      }
      containers: [{
        name: 'worker'
        image: image
        command: ['python', '-m', 'backend.saas_worker']
        resources: { cpu: json('1.0'), memory: '2Gi' }
        env: concat(pgEnvironment, [
          { name: 'PGUSER', value: 'staff_worker' }
          { name: 'PGPASSWORD', secretRef: 'pg-worker' }
        ])
      }]
    }
  }
}

output bootstrapJobName string = bootstrap.name
output appUrl string = deployApplication ? 'https://${app!.properties.configuration.ingress.fqdn}' : ''
output workerName string = deployWorker ? worker!.name : ''
