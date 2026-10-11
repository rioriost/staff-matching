targetScope = 'resourceGroup'

param location string = resourceGroup().location
param operatorObjectId string
@secure()
param postgresAdminPassword string
@secure()
param appDatabasePassword string
@secure()
param pocAccessPassword string

var suffix = take(uniqueString(resourceGroup().id), 8)
var tags = {
  project: 'staff-matching'
  environment: 'development'
  data: 'synthetic-only'
}
var pgName = 'psql-staff-${suffix}'
var appName = 'ca-staff-${suffix}'
var secretsUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-staff-${suffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.70.0.0/16']
    }
    subnets: [
      {
        name: 'container-apps'
        properties: {
          addressPrefix: '10.70.0.0/23'
          delegations: [{
            name: 'container-apps'
            properties: { serviceName: 'Microsoft.App/environments' }
          }]
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.70.2.0/24'
          delegations: [{
            name: 'postgres'
            properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' }
          }]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.70.3.0/27'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource dns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'staff-${suffix}.postgres.database.azure.com'
  location: 'global'
  tags: tags
}
resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dns
  name: 'staff-network'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

module postgres 'br/public:avm/res/db-for-postgre-sql/flexible-server:0.16.0' = {
  name: 'postgres'
  params: {
    name: pgName
    location: location
    tags: tags
    enableTelemetry: false
    administratorLogin: 'staffadmin'
    administratorLoginPassword: postgresAdminPassword
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    version: '17'
    skuName: 'Standard_B2s'
    tier: 'Burstable'
    availabilityZone: -1
    highAvailability: 'Disabled'
    backupRetentionDays: 7
    geoRedundantBackup: 'Disabled'
    autoGrow: 'Disabled'
    storageSizeGB: 32
    serverThreatProtection: 'Enabled'
    enableAdvancedThreatProtection: true
    publicNetworkAccess: 'Disabled'
    delegatedSubnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'postgres')
    privateDnsZoneArmResourceId: dns.id
    databases: [{ name: 'staffmatching' }]
    configurations: [
      { name: 'azure.extensions', source: 'user-override', value: 'age,postgis,vector,btree_gist' }
      { name: 'shared_preload_libraries', source: 'user-override', value: 'age,pg_stat_statements' }
    ]
  }
  dependsOn: [dnsLink]
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acrstaff${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}
resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-staff-app-${suffix}'
  location: location
  tags: tags
}
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-staff-setup-${suffix}'
  location: location
  tags: tags
}
resource appPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, appIdentity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource setupPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, setupIdentity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: 'kv-staff-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Disabled'
  }
}
resource vaultDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}
resource vaultDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: vaultDns
  name: 'staff-network'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}
resource vaultEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-staff-vault-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'private-endpoints')
    }
    privateLinkServiceConnections: [{
      name: 'vault'
      properties: {
        privateLinkServiceId: vault.id
        groupIds: ['vault']
      }
    }]
  }
}
resource vaultDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: vaultEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: 'vault'
      properties: { privateDnsZoneId: vaultDns.id }
    }]
  }
}
resource adminSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'postgres-admin-password'
  properties: { value: postgresAdminPassword }
}
resource databaseSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'app-database-password'
  properties: { value: appDatabasePassword }
}
resource authSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'poc-access-password'
  properties: { value: pocAccessPassword }
}
resource operatorSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorObjectId, secretsUserRole)
  scope: vault
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: operatorObjectId
    principalType: 'User'
  }
}
resource appDatabaseAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(databaseSecret.id, appIdentity.id, secretsUserRole)
  scope: databaseSecret
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource appAuthAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(authSecret.id, appIdentity.id, secretsUserRole)
  scope: authSecret
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource setupAdminAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(adminSecret.id, setupIdentity.id, secretsUserRole)
  scope: adminSecret
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource setupDatabaseAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(databaseSecret.id, setupIdentity.id, secretsUserRole)
  scope: databaseSecret
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-staff-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-staff-${suffix}'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'container-apps')
      internal: false
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    zoneRedundant: false
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

output connectionMetadata object = {
  postgresName: pgName
  postgresHost: '${pgName}.postgres.database.azure.com'
  databaseName: 'staffmatching'
  registryName: registry.name
  registryServer: registry.properties.loginServer
  vaultName: vault.name
  vaultUri: vault.properties.vaultUri
  environmentName: environment.name
  appIdentityName: appIdentity.name
  setupIdentityName: setupIdentity.name
  appName: appName
  bootstrapJobName: 'job-staff-setup-${suffix}'
}
