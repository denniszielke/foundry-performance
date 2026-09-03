
param location string = resourceGroup().location

@description('Delegate the agent subnet to Foundry so the agent service can be network injected')
param enablePrivateNetworking bool = false

@description('Service delegation used by the ACA sandbox VNet connection subnet')
param sandboxSubnetDelegation string = 'Microsoft.App/environments'

var agentSubnetName = 'agent-subnet'
var privateEndpointSubnetName = 'pe-subnet'
var sandboxSubnetName = 'sandbox-subnet'
var containerAppsSubnetName = 'aca-apps'

resource subnetNSG 'Microsoft.Network/networkSecurityGroups@2022-01-01' = {
  name: 'nsg-${resourceGroup().name}'
  location: location
  properties: {
    securityRules: [
      {
        name: 'allow-http-apim-all'
        properties: {
          description: 'apim http allow rules'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '80'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 2000
          direction: 'Inbound'
          }      
      }
      {
        name: 'allow-https-apim-all'
        properties: {
          description: 'apim https allow rules'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '443'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 2001
          direction: 'Inbound'
          }      
      }
      {
        name: 'allow-6390-apim-all'
        properties: {
          description: 'apim 6390 allow rules'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '6390'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 2002
          direction: 'Inbound'
          }      
      }
      {
        name: 'allow-3443-apim-all'
        properties: {
          description: 'apim 3443 allow rules'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '3443'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
          access: 'Allow'
          priority: 2003
          direction: 'Inbound'
          }      
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2021-05-01' = {
  name: 'vnet-${resourceGroup().name}'
  location: resourceGroup().location
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.0.0.0/19'
      ]
    }
    subnets: [
      {
        name: 'gateway'
        properties: {
          addressPrefix: '10.0.0.0/24'
          networkSecurityGroup: {
            id:  subnetNSG.id
          }
        }
      }
      {
        // Holds the private endpoints of the Foundry account and its storage
        // account when private networking is enabled.
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: '10.0.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
      }
      {
        // Network injection target for the Foundry agent service. The
        // delegation is only added in private mode because a delegated subnet
        // cannot be used for anything else.
        name: agentSubnetName
        properties: {
          addressPrefix: '10.0.2.0/24'
          delegations: enablePrivateNetworking ? [
            {
              name: 'Microsoft.app/environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ] : []
        }
      }
      {
        // Joined by the ACA sandbox group that runs the benchmark, so the
        // sandbox can reach privately deployed agents over the VNet.
        name: sandboxSubnetName
        properties: {
          addressPrefix: '10.0.3.0/24'
          delegations: [
            {
              name: 'sandbox-delegation'
              properties: {
                serviceName: sandboxSubnetDelegation
              }
            }
          ]
        }
      }
      {
        name: containerAppsSubnetName
        properties: {
          addressPrefix: '10.0.16.0/22'
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
          delegations: [
            {
              name: 'Microsoft.App.'
              properties: {
                serviceName: 'Microsoft.App/environments'
                actions: [
                  'Microsoft.Network/virtualNetworks/subnets/join/action'
                ]
              }
            }
          ]
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output agentSubnetId string = '${vnet.id}/subnets/${agentSubnetName}'
output privateEndpointSubnetId string = '${vnet.id}/subnets/${privateEndpointSubnetName}'
output sandboxSubnetId string = '${vnet.id}/subnets/${sandboxSubnetName}'
output containerAppsSubnetId string = '${vnet.id}/subnets/${containerAppsSubnetName}'
