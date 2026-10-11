targetScope = 'subscription'

param location string
param resourceGroupName string
param operatorObjectId string
@secure()
param postgresAdminPassword string
@secure()
param appDatabasePassword string
@secure()
param pocAccessPassword string

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    project: 'staff-matching'
    environment: 'development'
    data: 'synthetic-only'
  }
}

module resources './resources.bicep' = {
  name: 'staff-matching-resources'
  scope: rg
  params: {
    location: location
    operatorObjectId: operatorObjectId
    postgresAdminPassword: postgresAdminPassword
    appDatabasePassword: appDatabasePassword
    pocAccessPassword: pocAccessPassword
  }
}

output resourceGroupName string = rg.name
output resources object = resources.outputs.connectionMetadata
