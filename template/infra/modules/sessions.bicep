// The service's own session container in the platform's Cosmos DB account, and a data-plane
// role for the service identity scoped to that container only (no access to other teams' sessions).
// Deployed at the platform resource group scope.
param cosmosAccountName string
param databaseName string
param containerName string
param principalId string

// Built-in "Cosmos DB Built-in Data Contributor" (data plane, not an Azure RBAC role)
var dataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' existing = {
  parent: account
  name: databaseName
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: { paths: ['/id'], kind: 'Hash' }
      defaultTtl: -1 // per-item ttl, set by agentkit from AGENTKIT_SESSION_TTL_SECONDS
      indexingPolicy: {
        indexingMode: 'consistent'
        includedPaths: [{ path: '/owner/?' }]
        excludedPaths: [{ path: '/*' }] // sessions are read by id; don't pay to index history
      }
    }
  }
}

resource dataAccess 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, container.id, principalId, dataContributorRoleId)
  properties: {
    principalId: principalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    scope: '${account.id}/dbs/${databaseName}/colls/${containerName}'
  }
}

output endpoint string = account.properties.documentEndpoint
output containerName string = container.name
