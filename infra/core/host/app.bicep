param name string
param location string = resourceGroup().location
param tags object = {}

param containerAppsEnvironmentName string
param containerName string = 'main'
@description('Bare registry name (e.g. "myregistry"), without .azurecr.io')
param containerRegistryName string
param external bool = true
param imageName string
param targetPort int = 80

@description('User assigned identity name')
param identityName string

@description('JSON array of {name, value} env var objects, e.g. [{"name":"KEY","value":"VAL"}]')
param envJson string = '[]'

@description('CPU cores allocated to a single container instance, e.g. 0.5')
param containerCpuCoreCount string = '1'

@description('Memory allocated to a single container instance, e.g. 2.0Gi')
param containerMemory string = '2.0Gi'

@description('Minimum number of replicas (0 = scale to zero when idle)')
param minReplicas int = 0

@description('Maximum number of replicas for scale-out')
param maxReplicas int = 2

@description('Concurrent HTTP requests per replica before a new replica is added')
param concurrentRequests string = '10'

@description('HTTP path for the readiness probe. Leave empty to disable the probe.')
param readinessProbePath string = ''

var envVars = json(envJson)
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource userIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: containerRegistryName
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' existing = {
  name: containerAppsEnvironmentName
}

// Grant the user identity ACR pull access before the app is created.
// A user-assigned identity is required here: there is no way to pre-grant
// a system-assigned identity because it does not exist until the app is deployed.
resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, userIdentity.id, acrPullRoleDefinitionId)
  scope: containerRegistry
  properties: {
    roleDefinitionId: acrPullRoleDefinitionId
    principalId: userIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2023-05-01' = {
  name: name
  location: location
  tags: tags
  dependsOn: [ acrPullRoleAssignment ]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${userIdentity.id}': {} }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: external
        targetPort: targetPort
        transport: 'auto'
      }
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: userIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          image: !empty(imageName) ? imageName : 'mcr.microsoft.com/k8se/quickstart:latest'
          name: containerName
          env: envVars
          resources: {
            cpu: json(containerCpuCoreCount)
            memory: containerMemory
          }
          probes: empty(readinessProbePath) ? [] : [
            {
              type: 'readiness'
              httpGet: {
                path: readinessProbePath
                port: targetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-rule'
            http: {
              metadata: {
                concurrentRequests: concurrentRequests
              }
            }
          }
        ]
      }
    }
  }
}

output defaultDomain string = containerAppsEnvironment.properties.defaultDomain
output imageName string = app.properties.template.containers[0].image
output name string = app.name
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
