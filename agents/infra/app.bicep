targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string
param environmentName string
param identityName string
param registryName string
param image string
@secure()
param databaseUrl string
param tenantId string
@description('Application/client GUID of the existing Entra API application; requestedAccessTokenVersion must be 2.')
param apiAudience string
@description('Operator-verified agent service principal object IDs mapped to stages 1 through 7. Empty denies all agents.')
param agentPrincipals object = {}
@description('Operator/runner service principal object IDs authorized for read-only GET /runs.')
param readerPrincipals array = []
@minValue(1)
param maxFiles int = 100

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

var hostname = '${appName}.${environment.properties.defaultDomain}'

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
      ]
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'backend'
          image: image
          command: ['uvicorn']
          args: ['osdu_cloud.app:create_app', '--factory', '--host', '0.0.0.0', '--port', '8080', '--workers', '1']
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'OSDU_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'OSDU_SOURCE_ROOT'
              value: '/mnt/source'
            }
            {
              name: 'OSDU_SCAN_SUBPATH'
              value: 'Data'
            }
            {
              name: 'OSDU_TENANT_ID'
              value: tenantId
            }
            {
              name: 'OSDU_API_AUDIENCE'
              value: apiAudience
            }
            {
              name: 'OSDU_AGENT_PRINCIPALS_JSON'
              value: string(agentPrincipals)
            }
            {
              name: 'OSDU_READER_PRINCIPALS_JSON'
              value: string(readerPrincipals)
            }
            {
              name: 'OSDU_ALLOWED_HOSTS'
              value: string([hostname])
            }
            {
              name: 'OSDU_SCHEMA_CATALOG_ROOT'
              value: '/tmp/osdu-schemas'
            }
            {
              name: 'OSDU_MAX_FILES'
              value: string(maxFiles)
            }
          ]
          volumeMounts: [
            {
              volumeName: 'source'
              mountPath: '/mnt/source'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
                httpHeaders: [
                  {
                    name: 'Host'
                    value: hostname
                  }
                ]
              }
              initialDelaySeconds: 30
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8080
                httpHeaders: [
                  {
                    name: 'Host'
                    value: hostname
                  }
                ]
              }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'source'
          storageType: 'AzureFile'
          storageName: 'source'
          mountOptions: 'ro'
        }
      ]
    }
  }
}

output backendUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output backendHostname string = app.properties.configuration.ingress.fqdn
output backendResourceId string = app.id
