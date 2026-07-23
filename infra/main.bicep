targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the environment that can be used as part of naming resource convention')
param environmentName string

@minLength(1)
@maxLength(90)
@description('Name of the resource group to use or create')
param resourceGroupName string = 'rg-${environmentName}'

@minLength(1)
@description('Primary location for all resources')
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'italynorth'
  'japaneast'
  'koreacentral'
  'northcentralus'
  'norwayeast'
  'polandcentral'
  'southafricanorth'
  'southcentralus'
  'southeastasia'
  'southindia'
  'spaincentral'
  'swedencentral'
  'switzerlandnorth'
  'uaenorth'
  'uksouth'
  'westus'
  'westus2'
  'westus3'
])
param location string

@metadata({azd: {
  type: 'location'
  usageName: [
    'OpenAI.GlobalStandard.gpt-5.4-mini,10'
  ]}
})
param aiDeploymentsLocation string

@description('Id of the user or app to assign application roles')
param principalId string

@description('Principal type of user or app')
param principalType string

@description('Optional. Name of an existing AI Services account within the resource group.')
param aiFoundryResourceName string = ''

@description('Optional. Name of the AI Foundry project.')
param aiFoundryProjectName string = 'aiproj-${environmentName}'

@description('Name of the chat model deployment to use')
param chatModelDeploymentName string = 'gpt-5.4-mini'

@description('OpenAI API version used by the hosted apps')
param openAiApiVersion string = '2024-05-01-preview'

@description('List of model deployments')
param aiProjectDeploymentsJson string = '[{"name":"gpt-5.4-mini","model":{"name":"gpt-5.4-mini","format":"OpenAI","version":"2026-03-17"},"sku":{"name":"GlobalStandard","capacity":1000}}]'

@description('List of connections')
param aiProjectConnectionsJson string = '[]'

@description('List of resources to create and connect to the AI project')
param aiProjectDependentResourcesJson string = '[]'

var aiProjectDeployments = json(aiProjectDeploymentsJson)
var aiProjectConnections = json(aiProjectConnectionsJson)
var aiProjectDependentResources = json(aiProjectDependentResourcesJson)

@description('Enable hosted agent deployment')
param enableHostedAgents bool

@description('Enable monitoring for the AI project')
param enableMonitoring bool = true

@description('Set to true to skip creating project connections that already exist (idempotent re-runs after partial failure)')
param skipConnectionCreation bool = false

@description('Set to true to skip creating role assignments that already exist (idempotent re-runs after partial failure)')
param skipRoleAssignments bool = false

var tags = {
  'azd-env-name': environmentName
}

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Add ACR if hosted agents are enabled
var hasAcr = contains(map(aiProjectDependentResources, r => r.resource), 'registry')
var dependentResources = (enableHostedAgents) && !hasAcr ? union(aiProjectDependentResources, [
  {
    resource: 'registry'
    connectionName: 'acr-connection'
  }
]) : aiProjectDependentResources

module aiProject 'core/ai/ai-project.bicep' = {
  scope: rg
  name: 'ai-project'
  params: {
    tags: tags
    location: aiDeploymentsLocation
    aiFoundryProjectName: aiFoundryProjectName
    principalId: principalId
    principalType: principalType
    existingAiAccountName: aiFoundryResourceName
    deployments: aiProjectDeployments
    connections: aiProjectConnections
    additionalDependentResources: dependentResources
    enableMonitoring: enableMonitoring
    enableHostedAgents: enableHostedAgents
    skipConnectionCreation: skipConnectionCreation
    skipRoleAssignments: skipRoleAssignments
  }
}

module vnet 'core/host/vnet.bicep' = {
  scope: rg
  name: 'vnet'
  params: {
    location: location
  }
}

module identity 'core/host/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: {
    name: 'id-${environmentName}'
    location: location
    tags: tags
  }
}

module containerAppsEnv 'core/host/container-apps-environment.bicep' = {
  scope: rg
  name: 'container-apps-environment'
  params: {
    name: 'cae-${environmentName}'
    location: location
    tags: tags
    logAnalyticsWorkspaceName: aiProject.outputs.logAnalyticsWorkspaceName
  }
  dependsOn: [
    vnet
  ]
}

output AZURE_AI_PROJECT_ID string = aiProject.outputs.projectId
output AZURE_AI_PROJECT_NAME string = aiProject.outputs.projectName
output AZURE_AI_PROJECT_ENDPOINT string = aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output AZURE_OPENAI_ENDPOINT string = aiProject.outputs.AZURE_OPENAI_ENDPOINT
output APPLICATIONINSIGHTS_CONNECTION_STRING string = aiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = chatModelDeploymentName
output AZURE_OPENAI_CHAT_DEPLOYMENT_NAME string = chatModelDeploymentName
output OPENAI_API_VERSION string = openAiApiVersion

// ACR + Container Apps hosting (weather MCP server + custom agent)
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = aiProject.outputs.dependentResources.registry.loginServer
output AZURE_REGISTRY string = aiProject.outputs.dependentResources.registry.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = aiProject.outputs.dependentResources.registry.name
output AZURE_CONTAINER_ENVIRONMENT_NAME string = containerAppsEnv.outputs.name
output AZURE_MANAGED_IDENTITY_NAME string = identity.outputs.identityName

output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
