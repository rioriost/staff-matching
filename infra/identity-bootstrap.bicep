targetScope = 'resourceGroup'

@secure()
param bootstrapSecret string

var suffix = take(uniqueString(resourceGroup().id), 8)
var secretsUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: 'kv-staff-${suffix}'
}
resource setupIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: 'id-staff-setup-${suffix}'
}
resource accountSeed 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'poc-account-bootstrap'
  properties: {
    value: bootstrapSecret
    contentType: 'application/octet-stream'
  }
}
resource setupSeedAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(accountSeed.id, setupIdentity.id, secretsUserRole)
  scope: accountSeed
  properties: {
    roleDefinitionId: secretsUserRole
    principalId: setupIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}
