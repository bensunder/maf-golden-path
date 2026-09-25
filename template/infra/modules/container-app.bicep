// The agent service on Azure Container Apps, with Entra ID (Easy Auth) in front of it.
param name string
param location string
param tags object = {}
param containerAppsEnvironmentId string
param identityId string
param identityClientId string
param registryServer string
param env array
@secure()
param appInsightsConnectionString string
param authClientId string = ''
param minReplicas int = 1
param maxReplicas int = 1

@description('Replaced by `azd deploy` with the built image.')
param image string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
      secrets: [
        { name: 'appinsights-connection-string', value: appInsightsConnectionString }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          env: env
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 15
            }
            {
              type: 'Readiness'
              httpGet: { path: '/readyz', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

// Easy Auth: validates Entra tokens and injects X-MS-CLIENT-PRINCIPAL-NAME, the header agentkit uses for identity.
resource auth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = if (!empty(authClientId)) {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
      excludedPaths: ['/healthz', '/readyz']
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [authClientId, 'api://${authClientId}']
        }
      }
    }
    login: {
      tokenStore: { enabled: false }
    }
  }
}

output name string = app.name
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
output identityClientId string = identityClientId
