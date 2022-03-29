from agents.models import Agent
from clients.models import Client, Site
from django.db import models
from logs.models import BaseAuditModel

from typing import Optional


class Policy(BaseAuditModel):
    name = models.CharField(max_length=255, unique=True)
    desc = models.CharField(max_length=255, null=True, blank=True)
    active = models.BooleanField(default=False)
    enforced = models.BooleanField(default=False)
    alert_template = models.ForeignKey(
        "alerts.AlertTemplate",
        related_name="policies",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    excluded_sites = models.ManyToManyField(
        "clients.Site", related_name="policy_exclusions", blank=True
    )
    excluded_clients = models.ManyToManyField(
        "clients.Client", related_name="policy_exclusions", blank=True
    )
    excluded_agents = models.ManyToManyField(
        "agents.Agent", related_name="policy_exclusions", blank=True
    )

    def save(self, *args, **kwargs):
        from alerts.tasks import cache_agents_alert_template

        # get old policy if exists
        old_policy = type(self).objects.get(pk=self.pk) if self.pk else None
        super(Policy, self).save(old_model=old_policy, *args, **kwargs)

        # check if alert template was changes and cache on agents
        if old_policy:
            if old_policy.alert_template != self.alert_template:
                cache_agents_alert_template.delay()

    def __str__(self):
        return self.name

    @property
    def is_default_server_policy(self):
        return self.default_server_policy.exists()  # type: ignore

    @property
    def is_default_workstation_policy(self):
        return self.default_workstation_policy.exists()  # type: ignore

    def is_agent_excluded(self, agent):
        # will prefetch the many to many relations in a single query versus 3. esults are cached on the object
        models.prefetch_related_objects(
            [self], "excluded_agents", "excluded_sites", "excluded_clients"
        )

        return (
            agent in self.excluded_agents.all()
            or agent.site in self.excluded_sites.all()
            or agent.client in self.excluded_clients.all()
        )

    def related_agents(
        self, mon_type: Optional[str] = None
    ) -> "models.QuerySet[Agent]":
        models.prefetch_related_objects(
            [self],
            "excluded_agents",
            "excluded_sites",
            "excluded_clients",
            "workstation_clients",
            "server_clients",
            "workstation_sites",
            "server_sites",
            "agents",
        )

        agent_filter = {}
        filtered_agents_ids = Agent.objects.none()

        if mon_type:
            agent_filter["monitoring_type"] = mon_type

        excluded_clients_ids = self.excluded_clients.only("pk").values_list(
            "id", flat=True
        )
        excluded_sites_ids = self.excluded_sites.only("pk").values_list("id", flat=True)
        excluded_agents_ids = self.excluded_agents.only("pk").values_list(
            "id", flat=True
        )

        if self.is_default_server_policy:
            filtered_agents_ids |= (
                Agent.objects.exclude(block_policy_inheritance=True)
                .exclude(site__block_policy_inheritance=True)
                .exclude(site__client__block_policy_inheritance=True)
                .exclude(id__in=excluded_agents_ids)
                .exclude(site_id__in=excluded_sites_ids)
                .exclude(site__client_id__in=excluded_clients_ids)
                .filter(monitoring_type="server")
                .only("id")
                .values_list("id", flat=True)
            )

        if self.is_default_workstation_policy:
            filtered_agents_ids |= (
                Agent.objects.exclude(block_policy_inheritance=True)
                .exclude(site__block_policy_inheritance=True)
                .exclude(site__client__block_policy_inheritance=True)
                .exclude(id__in=excluded_agents_ids)
                .exclude(site_id__in=excluded_sites_ids)
                .exclude(site__client_id__in=excluded_clients_ids)
                .filter(monitoring_type="workstation")
                .only("id")
                .values_list("id", flat=True)
            )

        # if this is the default policy for servers and workstations and skip the other calculations
        if self.is_default_server_policy and self.is_default_workstation_policy:
            return Agent.objects.filter(models.Q(id__in=filtered_agents_ids))

        explicit_agents = (
            self.agents.filter(**agent_filter)  # type: ignore
            .exclude(id__in=excluded_agents_ids)
            .exclude(site_id__in=excluded_sites_ids)
            .exclude(site__client_id__in=excluded_clients_ids)
        )

        explicit_clients_qs = Client.objects.none()
        explicit_sites_qs = Site.objects.none()

        if not mon_type or mon_type == "workstation":
            explicit_clients_qs |= self.workstation_clients.exclude(  # type: ignore
                id__in=excluded_clients_ids
            )
            explicit_sites_qs |= self.workstation_sites.exclude(  # type: ignore
                id__in=excluded_sites_ids
            )

        if not mon_type or mon_type == "server":
            explicit_clients_qs |= self.server_clients.exclude(  # type: ignore
                id__in=excluded_clients_ids
            )
            explicit_sites_qs |= self.server_sites.exclude(  # type: ignore
                id__in=excluded_sites_ids
            )

        filtered_agents_ids |= (
            Agent.objects.exclude(block_policy_inheritance=True)
            .filter(
                site_id__in=[
                    site.id
                    for site in explicit_sites_qs
                    if site.client not in explicit_clients_qs
                    and site.client.id not in excluded_clients_ids
                ],
                **agent_filter,
            )
            .only("id")
            .values_list("id", flat=True)
        )

        filtered_agents_ids |= (
            Agent.objects.exclude(block_policy_inheritance=True)
            .exclude(site__block_policy_inheritance=True)
            .filter(
                site__client__in=explicit_clients_qs,
                **agent_filter,
            )
            .only("id")
            .values_list("id", flat=True)
        )

        return Agent.objects.filter(
            models.Q(id__in=filtered_agents_ids)
            | models.Q(id__in=explicit_agents.only("id"))
        )

    @staticmethod
    def serialize(policy):
        # serializes the policy and returns json
        from .serializers import PolicyAuditSerializer

        return PolicyAuditSerializer(policy).data

    @staticmethod
    def get_policy_tasks(agent):

        # List of all tasks to be applied
        tasks = list()

        # Get policies applied to agent and agent site and client
        policies = agent.get_agent_policies()

        processed_policies = list()

        for _, policy in policies.items():
            if policy and policy.active and policy.pk not in processed_policies:
                processed_policies.append(policy.pk)
                for task in policy.autotasks.all():
                    tasks.append(task)

        return tasks

    @staticmethod
    def get_policy_checks(agent):
        # Get checks added to agent directly
        agent_checks = list(agent.agentchecks.all())

        # Get policies applied to agent and agent site and client
        policies = agent.get_agent_policies()

        # Used to hold the policies that will be applied and the order in which they are applied
        # Enforced policies are applied first
        enforced_checks = list()
        policy_checks = list()

        processed_policies = list()

        for _, policy in policies.items():
            if policy and policy.active and policy.pk not in processed_policies:
                processed_policies.append(policy.pk)
                if policy.enforced:
                    for check in policy.policychecks.all():
                        enforced_checks.append(check)
                else:
                    for check in policy.policychecks.all():
                        policy_checks.append(check)

        # Sorted Checks already added
        added_diskspace_checks = list()
        added_ping_checks = list()
        added_winsvc_checks = list()
        added_script_checks = list()
        added_eventlog_checks = list()
        added_cpuload_checks = list()
        added_memory_checks = list()

        # Lists all agent and policy checks that will be returned
        diskspace_checks = list()
        ping_checks = list()
        winsvc_checks = list()
        script_checks = list()
        eventlog_checks = list()
        cpuload_checks = list()
        memory_checks = list()

        # Loop over checks in with enforced policies first, then non-enforced policies
        for check in enforced_checks + agent_checks + policy_checks:
            if check.check_type == "diskspace" and agent.plat == "windows":
                # Check if drive letter was already added
                if check.disk not in added_diskspace_checks:
                    added_diskspace_checks.append(check.disk)
                    # Dont add if check if it is an agent check
                    if not check.agent:
                        diskspace_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

            elif check.check_type == "ping":
                # Check if IP/host was already added
                if check.ip not in added_ping_checks:
                    added_ping_checks.append(check.ip)
                    # Dont add if the check if it is an agent check
                    if not check.agent:
                        ping_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

            elif check.check_type == "cpuload" and agent.plat == "windows":
                # Check if cpuload list is empty
                if not added_cpuload_checks:
                    added_cpuload_checks.append(check)
                    # Dont create the check if it is an agent check
                    if not check.agent:
                        cpuload_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

            elif check.check_type == "memory" and agent.plat == "windows":
                # Check if memory check list is empty
                if not added_memory_checks:
                    added_memory_checks.append(check)
                    # Dont create the check if it is an agent check
                    if not check.agent:
                        memory_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

            elif check.check_type == "winsvc" and agent.plat == "windows":
                # Check if service name was already added
                if check.svc_name not in added_winsvc_checks:
                    added_winsvc_checks.append(check.svc_name)
                    # Dont create the check if it is an agent check
                    if not check.agent:
                        winsvc_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

            elif check.check_type == "script" and agent.is_supported_script(
                check.script.supported_platforms
            ):
                # Check if script id was already added
                if check.script.id not in added_script_checks:
                    added_script_checks.append(check.script.id)
                    # Dont create the check if it is an agent check
                    if not check.agent:
                        script_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

            elif check.check_type == "eventlog" and agent.plat == "windows":
                # Check if events were already added
                if [check.log_name, check.event_id] not in added_eventlog_checks:
                    added_eventlog_checks.append([check.log_name, check.event_id])
                    if not check.agent:
                        eventlog_checks.append(check)
                elif check.agent:
                    check.overridden_by_policy = True
                    check.save()

        return (
            diskspace_checks
            + ping_checks
            + cpuload_checks
            + memory_checks
            + winsvc_checks
            + script_checks
            + eventlog_checks
        )
