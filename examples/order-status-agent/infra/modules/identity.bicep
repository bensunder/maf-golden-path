// User-assigned managed identity for the agent service: its only credential, for the gateway,
// Content Safety, the registry and any downstream APIs it calls.
param name string
param location string
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output id string = identity.id
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
