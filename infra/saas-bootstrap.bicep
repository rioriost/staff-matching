targetScope = 'resourceGroup'

param location string = resourceGroup().location
@secure()
param workerDatabasePassword string

var suffix = take(uniqueString(resourceGroup().id), 8)
var secretsUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: 'kv-staff-${suffix}'
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: 'acrstaff${suffix}'
}
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-staff-setup-${suffix}'
}
resource workerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-staff-worker-${suffix}'
  location: location
  tags: { project: 'staff-matching', environment: 'development', data: 'synthetic-only' }
}
resource databaseSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'worker-database-password'
  properties: {
    value: workerDatabasePassword
    contentType: 'application/octet-stream'
  }
}
resource workerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(databaseSecret.id, workerIdentity.id, secretsUserRole)
  scope: databaseSecret
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource setupAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(databaseSecret.id, setupIdentity.id, secretsUserRole)
  scope: databaseSecret
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
resource workerPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, workerIdentity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: workerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
